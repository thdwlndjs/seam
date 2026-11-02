"""출처 구조 지도: 루트 페이지 아래 섹션(큰 제목 단위)마다 역할과 수집 위치를 기억한다.

Notion API는 모른다. 어떤 섹션이 있는지는 server/notion_map.py가 스캔해서 여기에 저장하고,
사람은 `python -m worklog map` / `map-set`으로 역할을 확인·변경한다.

역할:
- progress  (팀원별 진행 기록) → 역할·작업으로 분석
- meeting   (회의록)           → 결정·작업으로 분석
- reference (멘토링, WBS 등)   → 원문만 보관, 분석 안 함
- unknown / ignore             → 수집 안 함

판정은 server/notion_map.py가 LLM으로 하고 바로 쓴다(별도 확인 단계 없음 — 사람 확인은 변경 후보 승인에서 한다).
- 애매한 섹션은 notion_map이 reference(보관만)로 정해 둔다.
- 이름이 그대로인 섹션은 재스캔해도 판정을 다시 하지 않는다.
- 사람이 map-set으로 정한 역할은 이름이 바뀌어도 유지되고, 보관해 둔 원문도 새 역할에 맞게 풀린다.
"""
from __future__ import annotations

import json
from contextlib import closing
from pathlib import Path
from typing import Any

from . import db
from .core import release
from .db import NotFoundError, ValidationError, to_json

ANALYZE_ROLES = {"progress", "meeting"}
ROLE_NAMES = {"progress": "진행 기록", "meeting": "회의", "reference": "보관만", "ignore": "수집 안 함", "unknown": "모름"}
# 명령에서 한국어로도 역할을 쓸 수 있게
ROLE_ALIASES = {"진행": "progress", "진행 기록": "progress", "회의": "meeting", "보관": "reference", "보관만": "reference",
                "참고": "reference", "제외": "ignore", "수집 안 함": "ignore", **{r: r for r in ("progress", "meeting", "reference", "ignore")}}
SOURCE_NAMES = {"llm": "LLM", "inherit": "상위 제목", "human": "사람", "none": "판정 못 함"}
ROLES = ANALYZE_ROLES | {"reference", "unknown", "ignore"}
SECTION_STATUSES = {"active", "inactive"}


def conversation_key(section_id: str) -> str:
    return f"notion:{section_id}"


def hold_for(section: dict[str, Any]) -> str | None | bool:
    """이 섹션의 원문을 어떻게 저장할지. False면 수집하지 않는다."""
    if section["status"] == "inactive" or section["role"] in {"unknown", "ignore"}:
        return False
    return "reference" if section["role"] == "reference" else None


def _section(row: Any) -> dict[str, Any]:
    item = dict(row)
    item["layout"] = json.loads(item.pop("layout_json"))
    return item


# ---------------------------------------------------------------- pages (구조 지문)


def pages(store: Path, scope: str) -> list[dict[str, Any]]:
    db.initialize(store)
    with closing(db.connect(store)) as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM source_pages WHERE scope = ? AND status = 'active' ORDER BY path", (scope,))]


def save_page(store: Path, scope: str, page_id: str, root_id: str, path: str, last_edited: str | None,
              fingerprint: str | None, status: str = "active") -> None:
    db.initialize(store)
    with db.write(store) as c:
        c.execute(
            """INSERT INTO source_pages(scope, page_id, root_id, path, last_edited, fingerprint, status, checked_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(scope, page_id) DO UPDATE SET root_id = excluded.root_id, path = excluded.path,
                 last_edited = excluded.last_edited, fingerprint = excluded.fingerprint, status = excluded.status,
                 checked_at = excluded.checked_at""",
            (scope, page_id, root_id, path, last_edited, fingerprint, status, db.now()),
        )


# ---------------------------------------------------------------- sections


def sections(store: Path, scope: str, page_id: str | None = None, include_inactive: bool = False) -> list[dict[str, Any]]:
    db.initialize(store)
    sql, args = "SELECT * FROM source_sections WHERE scope = ?", [scope]
    if page_id is not None:
        sql, args = sql + " AND page_id = ?", [*args, page_id]
    if not include_inactive:
        sql += " AND status != 'inactive'"
    with closing(db.connect(store)) as c:
        return [_section(r) for r in c.execute(sql + " ORDER BY path, rowid", args)]


def save_section(store: Path, scope: str, section: dict[str, Any]) -> dict[str, Any]:
    """스캔 결과 한 섹션을 저장한다.

    - 이름이 그대로인 기존 섹션: 위치 정보만 갱신하고, 역할·확인 상태는 그대로 둔다.
    - 새 섹션, 이름이 바뀐 섹션, 사라졌다 돌아온 섹션: 새 판정을 바로 쓴다.
    - 사람이 직접 정한 역할(map-set)은 이름이 바뀌어도 유지한다.
    """
    db.initialize(store)
    ts = db.now()
    with db.write(store) as c:
        old = c.execute("SELECT * FROM source_sections WHERE id = ?", (section["id"],)).fetchone()
        same = old is not None and old["title"] == section["title"] and old["status"] != "inactive"
        human = old is not None and old["role_source"] == "human" and old["status"] != "inactive"
        if same or human:
            c.execute("""UPDATE source_sections SET page_id = ?, path = ?, title = ?, layout_json = ?, updated_at = ?
                         WHERE id = ?""",
                      (section["page_id"], section["path"], section["title"], to_json(section["layout"]), ts, section["id"]))
        elif old is None:
            c.execute(
                """INSERT INTO source_sections(id, scope, page_id, path, title, role, role_source, role_note, status,
                                               layout_json, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)""",
                (section["id"], scope, section["page_id"], section["path"], section["title"], section["role"],
                 section["role_source"], section.get("role_note"), to_json(section["layout"]), ts, ts),
            )
        else:
            c.execute(
                """UPDATE source_sections SET page_id = ?, path = ?, title = ?, role = ?, role_source = ?, role_note = ?,
                                              status = 'active', decided_by = NULL, layout_json = ?, updated_at = ?
                   WHERE id = ?""",
                (section["page_id"], section["path"], section["title"], section["role"], section["role_source"],
                 section.get("role_note"), to_json(section["layout"]), ts, section["id"]),
            )
        return _section(c.execute("SELECT * FROM source_sections WHERE id = ?", (section["id"],)).fetchone())


def deactivate(store: Path, section_ids: list[str]) -> int:
    """사라진 섹션. 이미 쌓인 원문과 이력은 그대로 둔다."""
    if not section_ids:
        return 0
    db.initialize(store)
    with db.write(store) as c:
        marks = ",".join("?" * len(section_ids))
        return c.execute(f"UPDATE source_sections SET status = 'inactive', updated_at = ? WHERE id IN ({marks})",
                         (db.now(), *section_ids)).rowcount


def set_cursor(store: Path, section_id: str, cursor: str | None) -> None:
    with db.write(store) as c:
        c.execute("UPDATE source_sections SET cursor = ? WHERE id = ?", (cursor, section_id))


def _apply_role(store: Path, section_id: str, role: str) -> int:
    """사람이 정한 역할에 맞게 보류해 둔 원문을 분석 대기로 풀거나 보관으로 돌린다."""
    key = conversation_key(section_id)
    if role in ANALYZE_ROLES:
        return release(store, key, "unconfirmed") + release(store, key, "reference")
    with db.write(store) as c:  # 이미 쌓인 원문은 지우지 않고 보관으로만 돌린다
        c.execute("UPDATE work_records SET hold = 'reference' WHERE conversation_key = ? AND hold = 'unconfirmed'", (key,))
    return 0


def set_role(store: Path, section_id: str, role: str, actor: str) -> dict[str, Any]:
    """사람이 섹션 역할을 정한다(판정이 틀렸을 때만). 보관해 둔 원문도 새 역할에 맞게 풀거나 보관으로 돌린다.
    이 섹션에서 역할을 물려받은 하위 섹션(예: 팀원별 제목)도 같이 바뀐다."""
    if role not in ROLES:
        raise ValidationError(f"role must be one of {sorted(ROLES)}")
    if not actor or not actor.strip():
        raise ValidationError("actor must be a non-empty string")
    db.initialize(store)
    with db.write(store) as c:
        row = c.execute("SELECT * FROM source_sections WHERE id = ?", (section_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"section not found: {section_id}")
        children = [r["id"] for r in c.execute(
            """SELECT id FROM source_sections WHERE scope = ? AND role_source = 'inherit' AND status != 'inactive'
               AND substr(path, 1, ?) = ?""", (row["scope"], len(row["path"]) + 3, row["path"] + " > "))]
        c.execute("""UPDATE source_sections SET role = ?, role_source = 'human', role_note = NULL, status = 'active',
                                                decided_by = ?, updated_at = ? WHERE id = ?""",
                  (role, actor.strip(), db.now(), section_id))
        for child in children:
            c.execute("""UPDATE source_sections SET role = ?, role_note = ?, updated_at = ? WHERE id = ?""",
                      (role, f"상위 제목 '{row['title']}'에서 물려받음 ({actor.strip()}가 정함)", db.now(), child))
    released = sum(_apply_role(store, sid, role) for sid in [section_id, *children])
    with closing(db.connect(store)) as c:
        section = _section(c.execute("SELECT * FROM source_sections WHERE id = ?", (section_id,)).fetchone())
    return {"section": section, "inherited_by": children, "released_records": released}


# ---------------------------------------------------------------- units (하위 페이지·행)


def unit_edited(store: Path, unit_id: str) -> str | None:
    with closing(db.connect(store)) as c:
        row = c.execute("SELECT last_edited FROM source_units WHERE id = ?", (unit_id,)).fetchone()
        return row["last_edited"] if row else None


def save_unit(store: Path, section_id: str, unit_id: str, last_edited: str) -> None:
    with db.write(store) as c:
        c.execute("""INSERT INTO source_units(id, section_id, last_edited, collected_at) VALUES (?, ?, ?, ?)
                     ON CONFLICT(id) DO UPDATE SET section_id = excluded.section_id, last_edited = excluded.last_edited,
                     collected_at = excluded.collected_at""", (unit_id, section_id, last_edited, db.now()))


def summary(store: Path, scope: str) -> dict[str, Any]:
    """`python -m worklog map` 출력용."""
    items = []
    # 사람이 볼 만한 것부터: 애매해서 보관만 하는 섹션 → 나머지 → 사라진 섹션
    ordered = sorted(sections(store, scope, include_inactive=True),
                     key=lambda s: (s["status"] == "inactive", s["role_source"] not in {"none"} and "확신 낮음" not in (s.get("role_note") or ""),
                                    s["path"]))
    for s in ordered:
        layout = s["layout"]
        holds = hold_for(s)
        items.append({
            "id": s["id"], "path": s["path"], "role": s["role"], "role_source": s["role_source"],
            "why": s.get("role_note"), "status": s["status"], "decided_by": s.get("decided_by"),
            "contains": {k: len(v) for k, v in layout.items() if isinstance(v, list) and v},
            "collect": "no" if holds is False else ("analyze" if holds is None else f"keep ({holds})"),
            "unreadable": layout.get("unreadable", []),
        })
    return {"scope": scope, "pages": pages(store, scope), "sections": items}


def role_of(name: str) -> str:
    role = ROLE_ALIASES.get(name.strip())
    if role is None:
        raise ValidationError(f"역할은 다음 중 하나: {', '.join(sorted({k for k in ROLE_ALIASES if not k.isascii()}))} "
                              f"(또는 {', '.join(sorted(ANALYZE_ROLES | {'reference', 'ignore'}))})")
    return role


def resolve(store: Path, scope: str, key: str) -> dict[str, Any]:
    """섹션 ID, 섹션 이름, 또는 경로 끝부분("팀원별 진행상황 > 김팀원")으로 섹션 하나를 찾는다. 경로는 조각 단위로 맞춘다."""
    key = key.strip()
    active = sections(store, scope)
    for match in ([s for s in active if s["id"] == key], [s for s in active if s["title"] == key],
                  [s for s in active if s["path"] == key or s["path"].endswith(f" > {key}")]):
        if len(match) == 1:
            return match[0]
        if len(match) > 1:
            raise ValidationError("이름이 같은 섹션이 여러 개다. 경로를 더 길게 쓰거나 ID를 써라: "
                                  + "; ".join(f"{s['path']} ({s['id']})" for s in match))
    raise NotFoundError(f"{scope}에 '{key}' 섹션이 없다")


def role_table(store: Path, scope: str) -> list[dict[str, Any]]:
    """섹션별 역할만: 사람이 보고 고칠 때 쓰는 목록."""
    return [{"id": s["id"], "section": s["path"], "role": s["role"], "role_name": ROLE_NAMES.get(s["role"], s["role"]),
             "decided_by": s.get("decided_by") if s["role_source"] == "human" else SOURCE_NAMES.get(s["role_source"], s["role_source"]),
             "why": s.get("role_note")} for s in sections(store, scope)]
