"""구조 보기·고치기 탭의 데이터와 고치기. 문서: docs/v1.1/spec.md P2-8.

- 화면은 반영된 항목을 네 묶음으로 보인다: 확인 필요(needs_discussion) / 최종 결정 / 변경 이력(결정 대체) / 남은 쟁점·미정.
  할 일·인사이트·피드백은 그 아래 묶음으로 보인다.
- 항목마다 출처(어느 회의록·페이지의 어느 문장인지)와 이력(revisions)을 같이 준다.
- 고치기(종류·제목·담당·내용), 반영 취소, 확인 필요 답(확정·미정·무시)은 모두 revisions에 남는다.
  권한 확인은 호출부가 한다.

이 모듈은 Notion·LLM을 부르지 않는다.
"""

from __future__ import annotations

import json
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from . import apply, core, db, people, search, stages
from .db import ConflictError, NotFoundError, ValidationError

KINDS = {"decision": "결정", "requirement": "요구사항", "open": "미정", "hypothesis": "가설", "task": "할 일",
         "insight": "인사이트", "feedback": "피드백"}
ARTIFACT = "artifact"  # 산출물 문서: 목록은 따로 보이고, 종류를 바꿀 수 없다
MATURITY_LABELS = {"draft": "가안", "recommended": "조언", "discussed": "논의", "agreed": "합의"}
HIDDEN = ("superseded", "reverted", "cancelled", "resolved")
EDIT_REASON = "구조 화면에서 고침"
RESTORE_REASON = "구조 화면에서 복원"
DELETED_LIMIT = 30  # 삭제된 항목 보기에 보이는 최근 건수


_page_date = apply.date_from


def _notion_url(page_id: str | None, origin: str) -> str | None:
    return f"https://www.notion.so/{page_id.replace('-', '')}" if page_id and origin == "notion" else None


def _section_url(page_id: str | None, origin: str, block_ids_json: str | None) -> str | None:
    """부분이 시작하는 블록으로 바로 가는 Notion 링크(페이지 주소 + #블록ID). 블록 ID를 모르면 페이지 링크."""
    base = _notion_url(page_id, origin)
    try:
        blocks = json.loads(block_ids_json or "[]")
    except (TypeError, ValueError):
        blocks = []
    block = blocks[0] if isinstance(blocks, list) and blocks and isinstance(blocks[0], str) else ""
    return f"{base}#{block.replace('-', '')}" if base and block else base


def _sources(c: Any, scope: str) -> dict[str, dict[str, Any]]:
    """부분 ID → 출처(페이지 경로·날짜·링크)."""
    contents = {r["locator"]: r["content"] for r in c.execute(
        "SELECT locator, content FROM sources ORDER BY captured_at, rowid")}
    rows = c.execute("""SELECT id, origin, page_id, path, kind, last_edited, parent_part_id, block_ids_json FROM source_parts
                        WHERE scope = ?""", (scope,)).fetchall()
    # 부분 경로는 "페이지 경로 > 부분 제목"이다. 페이지 제목은 그 페이지의 가장 짧은 최상위 부분 경로에서 뗀다.
    page_paths: dict[str, str] = {}
    for row in rows:
        if row["parent_part_id"] is None and " > " in row["path"]:
            page = row["path"].rsplit(" > ", 1)[0]
            if len(page) < len(page_paths.get(row["page_id"], page + "_")):
                page_paths[row["page_id"]] = page
    out = {}
    for row in rows:
        locator = f"{row['origin']}:{row['page_id']}"
        page_path = page_paths.get(row["page_id"], row["path"])
        out[row["id"]] = {"part_id": row["id"], "page_id": row["page_id"], "origin": row["origin"], "path": row["path"],
                          "page_path": page_path, "title": page_path.split(" > ")[-1], "part_kind": row["kind"],
                          "date": _page_date(contents.get(locator), row["last_edited"]),
                          "url": _notion_url(row["page_id"], row["origin"]),
                          "section_url": _section_url(row["page_id"], row["origin"], row["block_ids_json"]),
                          "section": row["path"].rsplit(" > ", 1)[-1]}
    return out


def _quotes(c: Any, item_id: str) -> list[str]:
    rows = c.execute("""SELECT DISTINCT e.excerpt FROM change_candidates cc
                        JOIN object_evidence oe ON oe.object_type = 'change_candidate' AND oe.object_id = cc.id
                        JOIN evidence e ON e.id = oe.evidence_id
                        WHERE cc.applied_item_id = ? AND e.section_locator = 'quote'""", (item_id,)).fetchall()
    return [r["excerpt"] for r in rows]


def _history(c: Any, item_id: str) -> list[dict[str, Any]]:
    out = []
    for row in c.execute("""SELECT version, snapshot_json, reason, actor, changed_at FROM revisions
                            WHERE object_type = 'work_item' AND object_id = ? ORDER BY version""", (item_id,)):
        snap = json.loads(row["snapshot_json"])
        human = (row["reason"] or "").startswith(core.HUMAN_EDIT_PREFIX) and not (row["actor"] or "").startswith("자동(")
        out.append({"version": row["version"], "at": row["changed_at"], "actor": row["actor"], "reason": row["reason"],
                    "title": snap.get("title"), "kind": snap.get("kind"), "status": snap.get("status"), "by_human": human})
    return out


def _links(c: Any, scope: str) -> dict[str, list[dict[str, Any]]]:
    """항목 ID → 관계 목록. type: 관계 종류, direction: out(이 항목이 시작) / in(이 항목이 대상), other: 상대 항목."""
    titles = {r["id"]: {"id": r["id"], "title": r["title"], "kind": r["kind"], "status": r["status"]}
              for r in c.execute("SELECT id, title, kind, status FROM work_items WHERE scope = ?", (scope,))}
    out: dict[str, list[dict[str, Any]]] = {}
    for r in c.execute("SELECT source_id, target_id, type FROM work_links ORDER BY created_at, rowid"):
        if r["source_id"] in titles and r["target_id"] in titles:
            out.setdefault(r["source_id"], []).append({"type": r["type"], "direction": "out", "other": titles[r["target_id"]]})
            out.setdefault(r["target_id"], []).append({"type": r["type"], "direction": "in", "other": titles[r["source_id"]]})
    return out


def _github(attributes: dict[str, Any]) -> list[dict[str, Any]]:
    """GitHub 연결. basis: explicit(본문·커밋에 적힌 번호) / inferred(의미상 추정). 화면은 추정을 추정이라고 적는다."""
    return [{"type": r["type"], "number": int(r["number"]), "basis": r.get("basis", "explicit")} for r in core.github_refs(attributes)]


def _item(c: Any, row: Any, sources: dict[str, dict], links: dict[str, list] | None = None) -> dict[str, Any]:
    attributes = json.loads(row["attributes_json"])
    found = [sources[p] for p in attributes.get("part_ids") or [] if p in sources]
    quotes = _quotes(c, row["id"])
    mine = (links or {}).get(row["id"], [])
    maturity = attributes.get("maturity")
    return {"id": row["id"], "kind": row["kind"], "kind_label": KINDS.get(row["kind"], row["kind"]),
            "status": row["status"], "title": row["title"], "statement": row["statement_md"],
            "assignee": attributes.get("assignee"), "due": attributes.get("due"),
            "maturity": maturity if maturity in MATURITY_LABELS else None,
            "artifact_type": attributes.get("artifact_type"), "github": _github(attributes),
            "sources": sorted(found, key=lambda s: s["date"] or ""), "quotes": quotes,
            "from_document": bool(found) and all(s["part_kind"] == "document" for s in found),
            "source_missing": bool(attributes.get("source_missing")),
            "human_edited": core.is_human_edited(c, row["id"]),  # 사람이 고친 판: 재수집이 덮어쓰지 않는다
            "links": mine,
            "conflict": row["status"] == "needs_discussion" or any(link["type"] == core.LINK_RELATED_TO for link in mine),
            "updated_at": row["updated_at"], "history": _history(c, row["id"])}


def _artifact(c: Any, row: Any, sources: dict[str, dict], links: dict[str, list]) -> dict[str, Any]:
    """산출물 문서: 항목 모양 + 문서 날짜와 이 문서에서 나온 항목(DERIVED_FROM)."""
    item = _item(c, row, sources, links)
    derived = [link["other"] for link in item["links"]
               if link["type"] == core.LINK_DERIVED_FROM and link["direction"] == "in" and link["other"]["status"] not in HIDDEN]
    dates = [s["date"] for s in item["sources"] if s["date"]]
    return dict(item, derived=derived, date=max(dates) if dates else (row["updated_at"] or "")[:10])


def _changes(c: Any, scope: str) -> list[dict[str, Any]]:
    """결정 대체 이력(A → B). 되돌리기 할 수 있는 것은 candidate_id가 있다."""
    rows = c.execute("""SELECT cc.id, cc.decided_at, cc.status, w.id AS new_id, w.title AS new_title,
                               t.id AS old_id, t.title AS old_title FROM change_candidates cc
                        JOIN work_items w ON w.id = cc.applied_item_id JOIN work_items t ON t.id = cc.target_id
                        WHERE cc.scope = ? AND cc.change_type = 'superseding' AND cc.status IN ('approved', 'auto_applied')
                        ORDER BY cc.decided_at DESC, cc.rowid DESC""", (scope,)).fetchall()
    return [{"candidate_id": r["id"], "at": r["decided_at"], "old": {"id": r["old_id"], "title": r["old_title"]},
             "new": {"id": r["new_id"], "title": r["new_title"]}} for r in rows]


def snapshot(store: Path, scope: str, now: datetime | None = None) -> dict[str, Any]:
    """탭 한 화면에 필요한 것 전부."""
    db.initialize(store)
    since = ((now or datetime.now().astimezone()) - timedelta(hours=24)).astimezone().isoformat()
    with closing(db.connect(store)) as c:
        sources = _sources(c, scope)
        links = _links(c, scope)
        live = f"status NOT IN ({','.join('?' * len(HIDDEN))})"
        rows = c.execute(f"""SELECT * FROM work_items WHERE scope = ? AND {live}
                             AND kind IN ({",".join("?" * len(KINDS))}) ORDER BY updated_at DESC, rowid DESC""",
                         (scope, *HIDDEN, *KINDS)).fetchall()
        items = [_item(c, r, sources, links) for r in rows]
        artifacts = [_artifact(c, r, sources, links) for r in c.execute(
            f"SELECT * FROM work_items WHERE scope = ? AND kind = ? AND {live} ORDER BY updated_at DESC, rowid DESC",
            (scope, ARTIFACT, *HIDDEN))]
        deleted = [dict(_item(c, r, sources, links), deleted_at=r["updated_at"]) for r in c.execute(
            f"""SELECT * FROM work_items WHERE scope = ? AND status = 'cancelled' AND kind IN
                ({",".join("?" * (len(KINDS) + 1))}) ORDER BY updated_at DESC, rowid DESC LIMIT ?""",
            (scope, *KINDS, ARTIFACT, DELETED_LIMIT))]
        changes = _changes(c, scope)
        recent = c.execute("""SELECT COUNT(DISTINCT page_id) FROM source_parts WHERE scope = ? AND status = 'active'
                              AND state = 'applied' AND confirmed_at >= ?""", (scope, since)).fetchone()[0]
    used = {s["part_id"] for i in items for s in i["sources"]}
    pages: dict[str, dict] = {}
    for part_id in used:
        s = sources[part_id]
        pages.setdefault(s["page_id"] or part_id, {"page_id": s["page_id"] or part_id, "title": s["title"], "date": s["date"]})
    found = stages.board(store, scope)
    candidate = found["candidate"]  # 확정 > AI 추정(논의 중) > 게이트 기준: 메뉴 2·단계 카드와 같은 값
    return {"items": items, "artifacts": artifacts, "deleted": deleted, "changes": changes, "kinds": KINDS,
            "maturity": MATURITY_LABELS,
            "sources": sorted(pages.values(), key=lambda p: (p["date"] or "", p["title"])),
            "members": [m["display_name"] for m in people.team(store, scope) if m.get("display_name")],
            "stage": {"code": found["shown"], "label": stages.label(found["shown"]) if found["shown"] else None,
                      "approved": found["approved"], "doing": [g["text"] for g in found["doing"]],
                      "steps": [dict(s, gates=[g["text"] for g in stages.gates(store, scope, s["code"])])
                                for s in stages.stepper(candidate)] if candidate else []},
            "recent_notes": recent}


def _live(c: Any, scope: str, item_id: str) -> Any:
    row = c.execute("SELECT * FROM work_items WHERE id = ? AND scope = ?", (item_id, scope)).fetchone()
    if row is None:
        raise NotFoundError("항목을 찾을 수 없어요")
    if row["status"] in HIDDEN:
        raise ConflictError("이미 취소되거나 대체된 항목이에요")
    return row


def _clash(c: Any, scope: str, item_id: str, kind: str, title: str) -> None:
    other = c.execute(f"""SELECT id FROM work_items WHERE scope = ? AND kind = ? AND normalized_title = ? AND id != ?
                          AND status NOT IN ({",".join("?" * len(HIDDEN))})""",
                      (scope, kind, db.normalize_text(title), item_id, *HIDDEN)).fetchone()
    if other is not None:
        raise ConflictError(f"같은 제목의 {KINDS.get(kind, kind)} 항목이 이미 있어요")


def edit(store: Path, scope: str, item_id: str, changes: dict[str, Any], actor: str) -> dict[str, Any]:
    """종류·제목·담당·내용을 고친다. 바뀐 것이 없으면 그대로 둔다."""
    with db.write(store) as c:
        row = _live(c, scope, item_id)
        attributes = json.loads(row["attributes_json"])
        kind = changes.get("kind", row["kind"])
        if row["kind"] == ARTIFACT:
            if kind != ARTIFACT:
                raise ValidationError("산출물 문서는 종류를 바꿀 수 없어요")
        elif kind not in KINDS:
            raise ValidationError("알 수 없는 종류예요")
        title = (changes.get("title") if changes.get("title") is not None else row["title"]).strip()
        if not title:
            raise ValidationError("제목을 비울 수 없어요")
        update: dict[str, Any] = {}
        if kind != row["kind"]:
            update["kind"] = kind
            if kind == "task" and row["status"] not in core.TASK_STATUSES:
                update["status"] = "open"
            elif kind != "task" and row["status"] in core.TASK_STATUSES:
                update["status"] = "active"
        if title != row["title"]:
            update.update(title=title, normalized_title=db.normalize_text(title))
        if "statement" in changes and (changes["statement"] or "").strip() and changes["statement"].strip() != row["statement_md"]:
            update["statement_md"] = changes["statement"].strip()
        if "assignee" in changes:
            assignee = (changes["assignee"] or "").strip() or None
            if assignee != attributes.get("assignee"):
                if assignee:
                    attributes["assignee"] = assignee
                else:
                    attributes.pop("assignee", None)
                update["attributes_json"] = attributes
        if "maturity" in changes and changes["maturity"] != attributes.get("maturity"):
            if changes["maturity"] not in (*MATURITY_LABELS, None, ""):
                raise ValidationError("성숙도는 가안·조언·논의·합의 중 하나여야 해요")
            if changes["maturity"]:
                attributes["maturity"] = changes["maturity"]
            else:
                attributes.pop("maturity", None)
            update["attributes_json"] = attributes
        if not update:
            return {"id": item_id, "changed": False}
        if "kind" in update or "title" in update:
            _clash(c, scope, item_id, kind, title)
        core._write_item(c, item_id, update, EDIT_REASON, actor, db.now())
    return {"id": item_id, "changed": True}


def restore(store: Path, scope: str, item_id: str, actor: str) -> dict[str, Any]:
    """삭제(반영 취소)한 항목을 이력의 직전 상태로 되돌린다. 이력에 남고, 같은 제목의 살아 있는 항목이 있으면 막는다."""
    with db.write(store) as c:
        row = c.execute("SELECT * FROM work_items WHERE id = ? AND scope = ?", (item_id, scope)).fetchone()
        if row is None:
            raise NotFoundError("항목을 찾을 수 없어요")
        if row["status"] != "cancelled":
            raise ConflictError("삭제된 항목만 복원할 수 있어요")
        before = None
        for rev in c.execute("""SELECT snapshot_json FROM revisions WHERE object_type = 'work_item' AND object_id = ?
                                ORDER BY version DESC""", (item_id,)):
            snap = json.loads(rev["snapshot_json"])
            if snap.get("status") != "cancelled":
                before = snap
                break
        status = (before or {}).get("status") or ("open" if row["kind"] == "task" else "active")
        _clash(c, scope, item_id, row["kind"], row["title"])
        core._write_item(c, item_id, {"status": status}, RESTORE_REASON, actor, db.now())
    return {"id": item_id, "status": status}


def find(store: Path, scope: str, text: str, limit: int = 30) -> dict[str, Any]:
    """서버 검색(색인): 화면의 검색창이 쓴다. 삭제·되돌린·대체된 항목은 검색에서 빠진다. 찾은 ID를 점수 순으로 돌려준다."""
    found = search.query(store, scope, text, limit=limit, max_tokens=search.DEFAULT_MAX_TOKENS)
    return {"query": text, "ids": [r["id"] for r in found["records"]], "searched": found["searched"]}


def cancel(store: Path, scope: str, item_id: str, actor: str) -> dict[str, Any]:
    """반영 취소: 항목을 보이지 않게 한다(이력은 남는다)."""
    with db.write(store) as c:
        _live(c, scope, item_id)
        core._write_item(c, item_id, {"status": "cancelled"}, "구조 화면에서 반영 취소", actor, db.now())
    return {"id": item_id, "status": "cancelled"}


def resolve(store: Path, scope: str, item_id: str, answer: str, actor: str) -> dict[str, Any]:
    """확인 필요 항목의 답. 확정: 그 종류로 확정(미정이었으면 결정) / 미정: 미정으로 / 무시: 취소."""
    if answer == "ignore":
        return cancel(store, scope, item_id, actor)
    if answer not in ("confirm", "open"):
        raise ValidationError("확정·미정·무시 중 하나여야 해요")
    with db.write(store) as c:
        row = _live(c, scope, item_id)
        kind = "open" if answer == "open" else ("decision" if row["kind"] == "open" else row["kind"])
        if kind != row["kind"]:
            _clash(c, scope, item_id, kind, row["title"])
        status = "open" if kind == "task" else "active"
        reason = "구조 화면에서 확정" if answer == "confirm" else "구조 화면에서 미정으로 둠"
        core._write_item(c, item_id, {"kind": kind, "status": status}, reason, actor, db.now())
    return {"id": item_id, "kind": kind, "status": status}


def revert_change(store: Path, scope: str, candidate_id: str, actor: str) -> dict[str, Any]:
    with closing(db.connect(store)) as c:
        row = c.execute("SELECT scope FROM change_candidates WHERE id = ?", (candidate_id,)).fetchone()
    if row is None or row["scope"] != scope:
        raise NotFoundError("변경 이력을 찾을 수 없어요")
    return apply.revert(store, candidate_id, actor)
