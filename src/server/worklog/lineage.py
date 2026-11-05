"""산출물(artifact) 항목과 출처 연결. 문서: docs/implementation/v2.1/plan.md Phase 2.

- 해석한 문서(페이지)마다 산출물 항목 하나: 제목 = 페이지 제목, 본문 = 부분별 한 줄 요약 + 원문 링크, 속성 = 유형·성숙도·출처 부분.
- 그 문서에서 나온 항목(결정·미정 등)은 산출물에 DERIVED_FROM으로 이어진다(성숙도는 항목 속성, 원문 인용은 항목 근거).
- 사람이 구조 화면에서 고친 산출물은 다시 해석해도 덮어쓰지 않고, 사람이 지운(취소·되돌림) 것은 다시 만들지 않는다.
- 옮겨진 출처: 부분이 Notion에서 사라지면 블록 ID나 원문 인용으로 새 위치를 찾아 출처를 고치고, 못 찾으면 "출처 없음"으로 표시한다.
"""

from __future__ import annotations

import json
from contextlib import closing
from pathlib import Path
from typing import Any

from . import apply, core, db, parts, sections

AUTO_ACTOR = "자동(산출물 해석)"
MOVE_ACTOR = "자동(출처 이동)"
NOTION_URL = "https://www.notion.so/{}"


def _attributes(row: Any) -> dict[str, Any]:
    return json.loads(row["attributes_json"])


def _lowest(maturities: list[str | None]) -> str | None:
    known = [m for m in maturities if m in core.MATURITY]
    return min(known, key=core.MATURITY.index) if known else None


def _artifact_type(part_rows: list[dict[str, Any]], found: list[dict[str, Any]]) -> str:
    for row in part_rows:  # 사람이 올릴 때 고른 종류가 먼저다
        if row.get("kind_note") in parts.ARTIFACT_TYPES:
            return row["kind_note"]
    counts: dict[str, int] = {}
    for artifact in found:
        counts[artifact["type"]] = counts.get(artifact["type"], 0) + 1
    return max(counts, key=lambda k: counts[k]) if counts else "기타"


def _statement(page_id: str, title: str, lines: list[str]) -> str:
    return "\n".join([*lines, "", f"원문: {NOTION_URL.format(page_id.replace('-', ''))}"]).strip()


def sync_artifacts(store: Path, scope: str, prompt_version: str) -> dict[str, int]:
    """추출이 끝난 문서(회의 메모가 아닌 해석 대상)마다 산출물 항목을 만들거나 갱신하고, 그 문서에서 나온 항목을 잇는다."""
    db.initialize(store)
    catalog = {s["source_id"]: s for s in sections.catalog(store, scope, include_all=True)}
    by_page: dict[str, list[dict[str, Any]]] = {}
    for part in parts.interpretable(store, scope):
        if part["kind"] != "meeting" and part["page_id"]:
            by_page.setdefault(part["page_id"], []).append(part)
    stats = {"created": 0, "updated": 0, "protected": 0, "linked": 0}
    for page_id, page_parts in by_page.items():
        found: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for part in page_parts:
            artifact = apply.extracted_artifact(store, part, prompt_version)
            if artifact:
                found.append((part, artifact))
        if not found:
            continue
        title = (catalog.get(page_id) or {}).get("title") or found[0][0]["path"].split(" > ")[0]
        lines = [f"- {part['path'].rsplit(' > ', 1)[-1]}: {artifact['summary']}" for part, artifact in found]
        statement = _statement(page_id, title, lines)
        part_ids = sorted(p["id"] for p, _ in found)
        kind = _artifact_type([p for p, _ in found], [a for _, a in found])
        maturity = _lowest([a.get("maturity") for _, a in found])
        with db.write(store) as c:
            source = apply._source(c, found[0][0])
            existing = next((r for r in c.execute("SELECT * FROM work_items WHERE scope = ? AND kind = 'artifact'", (scope,))
                             if _attributes(r).get("page_id") == page_id), None)
            ts = db.now()
            if existing is None:
                clash = c.execute("SELECT 1 FROM work_items WHERE scope = ? AND kind = 'artifact' AND normalized_title = ?",
                                  (scope, db.normalize_text(title))).fetchone()
                name = f"{title} ({page_id[:8]})" if clash else title
                proposal = {"title": name, "statement": statement, "artifact_type": kind, "summary": found[0][1]["summary"],
                            "maturity": maturity}
                item_id = core._create_item(c, scope, "artifact", proposal, "해석한 문서의 산출물", AUTO_ACTOR, ts)
                attributes = _attributes(c.execute("SELECT attributes_json FROM work_items WHERE id = ?", (item_id,)).fetchone())
                attributes.update(page_id=page_id, part_ids=part_ids, source_ids=[source[0]] if source else [])
                core._write_item(c, item_id, {"attributes_json": attributes}, "출처 연결", AUTO_ACTOR, ts)
                if source:
                    evidence_id = core.new_id()
                    c.execute("INSERT INTO evidence(id, source_id, section_locator, excerpt, evidence_kind) VALUES (?, ?, 'record', ?, 'observed')",
                              (evidence_id, source[0], source[1][:300]))
                    c.execute("INSERT OR IGNORE INTO object_evidence(object_type, object_id, evidence_id) VALUES ('work_item', ?, ?)",
                              (item_id, evidence_id))
                stats["created"] += 1
            elif existing["status"] in core.INACTIVE_STATUSES:
                continue  # 사람이 지운(취소·되돌린) 산출물은 다시 만들지 않는다
            elif core.is_human_edited(c, existing["id"]):
                stats["protected"] += 1
                item_id = existing["id"]
            else:
                item_id = existing["id"]
                attributes = _attributes(existing)
                changed = {k: v for k, v in {"part_ids": part_ids, "artifact_type": kind, "maturity": maturity,
                                             "summary": found[0][1]["summary"]}.items() if v and attributes.get(k) != v}
                if changed or existing["statement_md"] != statement:
                    attributes.update(changed)
                    core._write_item(c, item_id, {"statement_md": statement, "attributes_json": attributes},
                                     "문서를 다시 해석해 산출물 갱신", AUTO_ACTOR, ts)
                    stats["updated"] += 1
            stats["linked"] += _link_derived(c, scope, item_id, set(part_ids), ts)
    return stats


def _link_derived(c: Any, scope: str, artifact_id: str, part_ids: set[str], ts: str) -> int:
    """산출물의 부분에서 나온 살아 있는 항목을 DERIVED_FROM으로 잇는다(이미 있으면 그대로)."""
    made = 0
    for row in c.execute(f"SELECT id, attributes_json FROM work_items WHERE scope = ? AND kind != 'artifact' "
                         f"AND status NOT IN ({','.join('?' * len(core.INACTIVE_STATUSES))})", (scope, *core.INACTIVE_STATUSES)).fetchall():
        if part_ids & set(_attributes(row).get("part_ids") or []):
            before = c.total_changes
            core._link(c, row["id"], artifact_id, core.LINK_DERIVED_FROM, None, ts)
            made += c.total_changes - before
    return made


# ── 옮겨지거나 사라진 출처 ───────────────────────────────────

def _quotes(c: Any, item_id: str) -> list[str]:
    return [r["excerpt"] for r in c.execute(
        """SELECT e.excerpt FROM object_evidence oe JOIN evidence e ON e.id = oe.evidence_id
           WHERE oe.object_type IN ('work_item', 'change_candidate') AND e.section_locator = 'quote'
             AND (oe.object_id = ? OR oe.object_id IN (SELECT candidate_id FROM work_links WHERE source_id = ? AND candidate_id IS NOT NULL))""",
        (item_id, item_id))]


def reconcile_gone(store: Path, scope: str) -> dict[str, int]:
    """Notion에서 사라진 해석 대상 부분을 처리한다. 새 위치를 찾으면(블록 ID가 겹치거나, 항목의 원문 인용이 지금 있는
    부분에 그대로 있으면) 항목의 출처를 그 부분으로 고치고 옛 부분은 '옮겨짐'으로 둔다. 못 찾으면 "출처 없음"으로 표시만 한다
    (항목은 지우지 않는다. 지울지는 권한자가 묻는 흐름에서 정한다)."""
    db.initialize(store)
    stats = {"moved": 0, "missing": 0}
    chosen = parts.selected_ids(store, scope)
    with db.write(store) as c:
        gone = [dict(r) for r in c.execute(
            "SELECT * FROM source_parts WHERE scope = ? AND status = 'gone' AND state = 'applied'", (scope,))
            if r["kind"] == "meeting" or r["id"] in chosen]
        active = [dict(r) for r in c.execute("SELECT * FROM source_parts WHERE scope = ? AND status = 'active'", (scope,))]
        for old in gone:
            old_blocks = set(json.loads(old["block_ids_json"] or "[]"))
            items = [r for r in c.execute("SELECT * FROM work_items WHERE scope = ?", (scope,))
                     if old["id"] in (_attributes(r).get("part_ids") or [])]
            if not items:
                continue
            new_part = next((p for p in active if old_blocks and old_blocks & set(json.loads(p["block_ids_json"] or "[]"))), None)
            squashed = {p["id"]: parts._squash(p["md"]) for p in active}
            ts = db.now()
            for item in items:
                target = new_part
                if target is None:
                    quotes = [parts._squash(q) for q in _quotes(c, item["id"]) if q]
                    target = next((p for p in active if quotes and all(q in squashed[p["id"]] for q in quotes)), None)
                attributes = _attributes(item)
                if target is not None:
                    attributes["part_ids"] = sorted((set(attributes.get("part_ids") or []) - {old["id"]}) | {target["id"]})
                    attributes.pop("source_missing", None)
                    reason, stats_key = f"출처 이동: {old['path']} → {target['path']}", "moved"
                elif not attributes.get("source_missing"):
                    attributes["source_missing"] = True
                    reason, stats_key = f"출처 없음: {old['path']}", "missing"
                else:
                    continue
                core._write_item(c, item["id"], {"attributes_json": attributes}, reason, MOVE_ACTOR, ts)
                stats[stats_key] += 1
            moved_all = all(old["id"] not in (_attributes(c.execute("SELECT * FROM work_items WHERE id = ?", (r["id"],)).fetchone())
                                              .get("part_ids") or []) for r in items)
            if moved_all:
                c.execute("UPDATE source_parts SET state = 'moved', updated_at = ? WHERE id = ?", (ts, old["id"]))
    return stats
