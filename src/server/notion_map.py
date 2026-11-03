"""Notion 팀 정보 페이지 → 구조 지도 → 원문 수집.

팀마다 페이지 구성이 달라서, 내용을 읽기 전에 구조부터 파악한다.

1. 접근할 때마다 (내용은 안 읽음)
   - 지도에 있는 페이지마다 수정 시각만 확인한다 (페이지당 API 1번).
   - 바뀐 페이지만 최상위 블록 목록을 읽고 구조 지문(제목·하위 페이지·DB의 ID와 이름)을 비교한다.
   - 지문이 같으면 내용만 바뀐 것: 지도는 그대로 둔다. 다르면 그 페이지만 다시 스캔한다.
2. 스캔
   - 큰 제목(그 페이지에 쓰인 제목 단계 중 위의 두 단계)마다 섹션 하나.
   - 제목 아래 내용이 그대로 적혀 있으면 inline, 하위 페이지·인라인 DB·전체 페이지 DB는 그 위치를 기억한다.
   - 제목 밖에 놓인 하위 페이지는 그 안으로 들어가 같은 방식으로 훑는다 (최대 3단계).
   - 역할: 새로 생기거나 이름이 바뀐 섹션만 LLM이 판정한다(경로, 제목, 담는 방식, 내용 앞부분을 보고).
     판정은 바로 쓴다. 사람의 확인은 뒤쪽 변경 후보 승인에서 한다(같은 안전장치를 두 번 두지 않는다).
     애매한 것(확신 낮음, 판정 못 함, LLM 없음)은 분석하지 않고 원문만 보관한다. 상위 제목의 역할은 물려받는다.
     잘못된 판정은 `python -m worklog map-set`으로 고치면 되고, 보관해 둔 원문도 그에 맞게 풀린다.
   - 첫 제목 위의 글(팀 소개 등)은 "(머리말)" 섹션이 된다.
3. 수집
   - 모든 섹션의 원문을 저장한다. 분석은 진행 기록·회의 섹션만 하고, 나머지는 보관만 한다.
     저장된 원문은 1번 메뉴(팀/프로젝트 정보)가 Notion을 다시 읽지 않고 답하는 데 쓰인다.
   - 지도대로, 수정 시각이 바뀐 섹션·하위 페이지·DB 행만 본문까지 읽어 worklog에 원문으로 저장한다.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from notion_client import Client

import metrics
import notion_md
import worklog
from notion_read import _block_text, _fetch_notion_page_by_id, _list_child_blocks, _rich_text
from worklog import db as wdb
from worklog import parts as wparts
from worklog import sections as wsections

MAX_PAGE_DEPTH = 3
HEADINGS = {"heading_1": 1, "heading_2": 2, "heading_3": 3}
CONTAINERS = {"column_list", "column", "synced_block"}
UNREADABLE = {"unsupported"}  # 링크된 DB 보기 등 API가 내용을 주지 않는 블록
LINKS = {"link_to_page"}  # 다른 페이지·DB를 가리키는 블록: 하위 페이지·DB처럼 따라간다(spec P1-1)
LINK_TITLE = "(링크된 페이지)"

ROLE_LABELS = {"progress": "진행 기록", "meeting": "회의", "reference": "참고 자료"}

SAMPLE_CHARS = 200
SAMPLE_ROWS = 5

Classifier = Callable[[list[dict[str, Any]]], dict[str, Any]]


def _title(block: dict) -> str:
    kind = block["type"]
    if kind == "child_page":
        return block.get("child_page", {}).get("title", "")
    if kind == "child_database":
        return block.get("child_database", {}).get("title", "")
    if kind in LINKS:
        return LINK_TITLE
    return _rich_text(block.get(kind, {}))


def _link_target(block: dict) -> tuple[str, str] | None:
    """link_to_page 블록이 가리키는 것: ("page" | "database", ID). 그 밖(댓글 링크 등)은 None."""
    target = block.get("link_to_page", {}) or {}
    if target.get("type") == "page_id" and target.get("page_id"):
        return "page", target["page_id"]
    if target.get("type") == "database_id" and target.get("database_id"):
        return "database", target["database_id"]
    return None


def intro_id(page_id: str) -> str:
    return f"{page_id}:intro"


def _is_text(block: dict) -> bool:
    return block["type"] not in HEADINGS and block["type"] not in {"child_page", "child_database"} | UNREADABLE | CONTAINERS | LINKS


def _is_toggle(block: dict) -> bool:
    return bool(block.get(block["type"], {}).get("is_toggleable"))


def _page_title(meta: dict) -> str:
    for prop in meta.get("properties", {}).values():
        if prop.get("type") == "title":
            return "".join(t.get("plain_text", "") for t in prop.get("title", []))
    return ""


def _flatten(client: Client, block_id: str, depth: int) -> list[dict]:
    """열(column)·동기화 블록 안의 블록을 바깥 목록에 펼친다. 레이아웃일 뿐 섹션 경계가 아니라서."""
    out: list[dict] = []
    for block in _list_child_blocks(block_id, client):
        if block["type"] in CONTAINERS and block.get("has_children") and depth < 3:
            source = block.get("synced_block", {}).get("synced_from") if block["type"] == "synced_block" else None
            out.extend(_flatten(client, (source or {}).get("block_id") or block["id"], depth + 1))
        else:
            out.append(block)
    return out


def top_blocks(client: Client, page_id: str) -> list[dict]:
    return _flatten(client, page_id, 0)


def fingerprint(blocks: list[dict]) -> str:
    """구조 지문: 제목·하위 페이지·DB·읽을 수 없는 블록의 ID·종류·이름. 일반 문단은 넣지 않는다."""
    items = [(b["id"], b["type"], _title(b), _link_target(b) if b["type"] in LINKS else None) for b in blocks
             if b["type"] in HEADINGS or b["type"] in {"child_page", "child_database"} | UNREADABLE | LINKS]
    if not any(item[3] for item in items):
        items = [item[:3] for item in items]  # 링크 블록이 없으면 예전 지문과 같게 둔다(저장된 지도를 다시 스캔하지 않게)
    return wdb.sha256(items)


def scan_page(page_id: str, path: str, blocks: list[dict], depth: int) -> tuple[list[dict], list[tuple[str, str]]]:
    """블록 목록 → (섹션들, 안으로 더 들어가 볼 하위 페이지들)."""
    levels = sorted({HEADINGS[b["type"]] for b in blocks if b["type"] in HEADINGS})
    boundary = set(levels[:2])
    found: list[dict] = []
    subpages: list[tuple[str, str]] = []
    current: dict | None = None
    parent: dict | None = None  # 위 단계 제목 섹션 (아래 단계 제목의 경로·역할 상속용)

    def new_section(block_id: str, title: str, section_path: str, inline: bool, inherit: dict | None) -> dict:
        section = {"id": block_id, "page_id": page_id, "title": title or "(제목 없음)", "path": section_path,
                   "inherit_from": inherit["path"] if inherit else None, "inherit_id": inherit["id"] if inherit else None,
                   "layout": {"inline": inline, "pages": [], "databases": [], "unreadable": []}}
        found.append(section)
        return section

    # 첫 제목 위에 적힌 글(팀 소개 등)도 하나의 섹션으로 둔다. 1번 메뉴(팀 정보)에 필요하다.
    first = next((i for i, b in enumerate(blocks) if b["type"] in HEADINGS and HEADINGS[b["type"]] in boundary), len(blocks))
    if any(_is_text(b) for b in blocks[:first]):
        new_section(intro_id(page_id), "(머리말)", f"{path} > (머리말)", True, None)

    for block in blocks:
        kind = block["type"]
        if kind in LINKS:
            target = _link_target(block)
            if target is None:
                continue
            # 링크는 그 대상을 하위 페이지·DB처럼 다룬다. 다른 루트가 이미 덮은 페이지는 sync_roots가 뺀다.
            block = {"id": target[1], "type": "child_page" if target[0] == "page" else "child_database",
                     "child_page" if target[0] == "page" else "child_database": {"title": LINK_TITLE}}
            kind = block["type"]
        if kind in HEADINGS and HEADINGS[kind] in boundary:
            title = _title(block)
            if HEADINGS[kind] == levels[0]:
                parent = current = new_section(block["id"], title, f"{path} > {title}", True, None)
            else:
                base = parent["path"] if parent else path
                current = new_section(block["id"], title, f"{base} > {title}", True, parent)
            if _is_toggle(block):
                # 토글 제목의 내용은 토글 안쪽뿐이다. 뒤에 오는 블록은 이 섹션에 속하지 않는다.
                current = None if HEADINGS[kind] == levels[0] else parent
                if HEADINGS[kind] == levels[0]:
                    parent = None
            continue
        if kind == "child_page":
            item = {"id": block["id"], "title": _title(block)}
            if current is not None:
                current["layout"]["pages"].append(item)
            elif depth < MAX_PAGE_DEPTH:
                subpages.append((block["id"], item["title"]))
            else:
                new_section(block["id"], item["title"], f"{path} > {item['title']}", False, None)["layout"]["pages"].append(item)
        elif kind == "child_database":
            item = {"id": block["id"], "title": _title(block)}
            if current is not None:
                current["layout"]["databases"].append(item)
            else:
                new_section(block["id"], item["title"], f"{path} > {item['title']}", False, None)["layout"]["databases"].append(item)
        elif kind in UNREADABLE and current is not None:
            current["layout"]["unreadable"].append(block["id"])
    return found, subpages


def _describe(layout: dict) -> str:
    parts = []
    if layout.get("inline"):
        parts.append("제목 아래 글")
    if layout.get("pages"):
        parts.append(f"하위 페이지 {len(layout['pages'])}개")
    if layout.get("databases"):
        parts.append(f"데이터베이스 {len(layout['databases'])}개")
    if layout.get("unreadable"):
        parts.append("읽을 수 없는 블록(링크된 DB 보기 등)")
    return ", ".join(parts) or "비어 있음"


def _plain(block: dict) -> str:
    return _rich_text(block.get(block["type"], {})) if block["type"] not in {"child_page", "child_database"} else _title(block)


def _sample(client: Client, section: dict, blocks: list[dict]) -> str:
    """판정용 내용 앞부분. 본문을 재귀로 읽지 않고, 이미 받은 블록과 DB 앞쪽 몇 행만 본다."""
    layout = section["layout"]
    pieces: list[str] = []
    index = -1 if section["id"].endswith(":intro") else next((i for i, b in enumerate(blocks) if b["id"] == section["id"]), None)
    if index == -1:
        boundary = set(sorted({HEADINGS[b["type"]] for b in blocks if b["type"] in HEADINGS})[:2])
        for block in blocks:
            if block["type"] in HEADINGS and HEADINGS[block["type"]] in boundary:
                break
            pieces.append(_plain(block))
    elif index is not None and layout.get("inline"):
        heading = blocks[index]
        if _is_toggle(heading):
            pieces += [_plain(b) for b in _list_child_blocks(heading["id"], client)]
        else:
            boundary = set(sorted({HEADINGS[b["type"]] for b in blocks if b["type"] in HEADINGS})[:2])
            for block in blocks[index + 1:]:
                if block["type"] in HEADINGS and HEADINGS[block["type"]] in boundary:
                    break
                pieces.append(_plain(block))
    pieces += [f"(하위 페이지) {p['title']}" for p in layout.get("pages", [])[:SAMPLE_ROWS]]
    for database in layout.get("databases", [])[:2]:
        try:
            source_id = client.databases.retrieve(database["id"])["data_sources"][0]["id"]
            rows = client.data_sources.query(source_id, page_size=SAMPLE_ROWS)["results"]
        except Exception:
            rows = []
        pieces += [f"(행) {_row_text(r).splitlines()[0]}" for r in rows[:SAMPLE_ROWS] if _row_text(r)]
    return " / ".join(p for p in pieces if p.strip())[:SAMPLE_CHARS]


def _read_answer(answer: Any) -> tuple[str | None, str]:
    if isinstance(answer, str):
        return answer, ""
    if isinstance(answer, dict):
        note = " · ".join(str(answer[k]) for k in ("confidence", "reason") if answer.get(k))
        return answer.get("role"), note
    return None, ""


def _judge_sections(store: Path, scope: str, client: Client, found: list[dict], blocks: list[dict], page_path: str,
                    classify: Classifier | None, rejudge_unknown: bool = False) -> int:
    """새로 생기거나 이름이 바뀐 섹션만 역할을 판정한다. 나머지는 저장된 판정을 그대로 쓴다. 판정한 수를 돌려준다."""
    stored = {s["id"]: s for s in wsections.sections(store, scope, include_inactive=True)}

    def settled(section: dict) -> bool:
        old = stored.get(section["id"])
        if old is None or old["status"] == "inactive":
            return False
        if old["role_source"] == "none" and rejudge_unknown:
            return False  # reindex: "판정 못 함"(LLM 없음·실패)으로 굳은 섹션을 다시 판정한다(spec P1-10)
        return old["title"] == section["title"] or old["role_source"] == "human"  # 사람이 정한 역할은 이름이 바뀌어도 유지

    need = [s for s in found if not settled(s)]
    if not need:
        return 0
    answers: dict[str, Any] = {}
    if classify is not None:
        items = [{"id": s["id"], "path": s["path"], "title": s["title"], "container": _describe(s["layout"]),
                  "sample": _sample(client, s, blocks)} for s in need]
        try:
            answers = classify(items) or {}
        except Exception:
            answers = {}
    by_path = {s["path"]: s for s in found}
    for section in need:
        role, note = _read_answer(answers.get(section["id"]))
        confidence = (answers.get(section["id"]) or {}).get("confidence") if isinstance(answers.get(section["id"]), dict) else None
        if role in wsections.ROLES - {"unknown", "reference"} and confidence == "low":
            # 애매하면 분석하지 않고 원문만 보관한다. 나중에 map-set으로 고치면 보관분이 분석 대기로 풀린다.
            section.update(role="reference", role_source="llm", role_note=f"확신 낮음({role}) → 보관만 · {note}")
        elif role in wsections.ROLES - {"unknown"}:
            section.update(role=role, role_source="llm", role_note=note)
        elif (parent := by_path.get(section["inherit_from"]) or stored.get(section.get("inherit_id") or "")) \
                and parent.get("role") not in {None, "unknown"}:
            section.update(role=parent["role"], role_source="inherit", role_note=f"상위 제목 '{parent['title']}'에서 물려받음")
        else:
            section.update(role="reference", role_source="none", role_note="판정 못 함(LLM 없음·실패) → 보관만")
    return len(need)


def _row_text(row: dict) -> str:
    lines = []
    for name, prop in row.get("properties", {}).items():
        kind = prop.get("type")
        value: Any = prop.get(kind)
        if kind in {"title", "rich_text"}:
            text = "".join(t.get("plain_text", "") for t in value or [])
        elif kind in {"select", "status"}:
            text = (value or {}).get("name", "")
        elif kind == "multi_select":
            text = ", ".join(v.get("name", "") for v in value or [])
        elif kind == "people":
            text = ", ".join(v.get("name") or v.get("id", "") for v in value or [])
        elif kind == "date":
            text = "~".join(filter(None, [(value or {}).get("start"), (value or {}).get("end")]))
        elif kind in {"number", "checkbox", "url", "email"}:
            text = "" if value is None else str(value)
        else:
            continue
        if text:
            lines.append(f"{name}: {text}")
    return "\n".join(lines)


def _query_rows(client: Client, database_id: str, since: str | None) -> list[dict]:
    """마지막 수집 이후 수정된 행만. 필터가 안 먹으면 전체를 읽는다(행별 수정 시각으로 다시 거른다)."""
    source_id = client.databases.retrieve(database_id)["data_sources"][0]["id"]
    flt = {"timestamp": "last_edited_time", "last_edited_time": {"on_or_after": since}} if since else None
    rows: list[dict] = []
    cursor = None
    while True:
        kwargs: dict[str, Any] = {"start_cursor": cursor}
        if flt:
            kwargs["filter"] = flt
        try:
            response = client.data_sources.query(source_id, **kwargs)
        except Exception:
            if not flt:
                raise
            flt = None
            rows, cursor = [], None
            continue
        rows.extend(response["results"])
        if not response.get("has_more"):
            return rows
        cursor = response.get("next_cursor")


def _inline_text(client: Client, blocks: list[dict], section_id: str) -> str:
    """제목 블록부터 다음 같은 급(이상) 제목 전까지. 하위 페이지·DB는 따로 모으므로 뺀다."""
    boundary = set(sorted({HEADINGS[b["type"]] for b in blocks if b["type"] in HEADINGS})[:2])
    if section_id.endswith(":intro"):  # 머리말: 페이지 처음부터 첫 제목 전까지
        index, lines = -1, []
    else:
        index = next((i for i, b in enumerate(blocks) if b["id"] == section_id), None)
        if index is None:
            return ""
        heading = blocks[index]
        lines = _block_text(heading, client)
        if _is_toggle(heading):
            return "\n".join(line for line in lines if line.strip()) if len(lines) > 1 else ""  # 내용은 토글 안쪽뿐
    for block in blocks[index + 1:]:
        if block["type"] in HEADINGS and HEADINGS[block["type"]] in boundary:
            break
        if block["type"] in {"child_page", "child_database"} | UNREADABLE | LINKS:
            continue
        lines.extend(_block_text(block, client))
    body = [line for line in lines if line.strip()]
    if index >= 0 and len(body) <= 1:
        return ""  # 제목만 있고 내용이 없으면 저장할 원문이 없다
    return "\n".join(body)


def _covered_ids(store: Path, scope: str, other_than_root: str) -> set[str]:
    """지도에 이미 들어 있는 페이지·하위 페이지·DB의 ID(비교용 표기). other_than_root 루트 자신의 지도는 뺀다."""
    other = wdb.notion_id(other_than_root)
    ids = {wdb.notion_id(p["page_id"]) for p in wsections.pages(store, scope) if wdb.notion_id(p["root_id"]) != other}
    for section in wsections.sections(store, scope):
        if wdb.notion_id(section["page_id"]) not in ids:
            continue
        ids.add(wdb.notion_id(section["id"]))
        ids.update(wdb.notion_id(item["id"]) for key in ("pages", "databases") for item in section["layout"].get(key, []))
    return ids


def _retire_root(store: Path, scope: str, root: str, stats: dict[str, Any]) -> None:
    """루트 하나의 지도를 정리한다(쌓인 원문과 이력은 남는다)."""
    for page in [p for p in wsections.pages(store, scope) if wdb.notion_id(p["root_id"]) == wdb.notion_id(root)]:
        wsections.save_page(store, scope, page["page_id"], page["root_id"], page["path"], page["last_edited"],
                            page["fingerprint"], status="gone")
        stats["sections_removed"] += _remove_page_sections(store, scope, page["page_id"], keep=set())


def sync_roots(store: Path, scope: str, roots: list[str], client: Client, classify: Classifier | None = None,
               classify_parts: wparts.PartClassifier | None = None, rejudge_unknown: bool = False) -> dict[str, Any]:
    """루트 여러 개(팀 정보 페이지, 회의록 페이지)를 차례로 맞춘다.

    - 첫 루트(팀 정보 페이지)가 기준이다. 다음 루트가 이미 그 안에 들어 있으면(하위 페이지·DB로) 따로 읽지 않는다.
      그 루트를 예전에 따로 읽어 둔 지도가 있으면 정리한다(같은 행을 두 섹션이 번갈아 차지하지 않게).
    - 설정에서 빠진 루트의 지도는 정리한다(쌓인 원문과 이력은 남는다).
    - 설정에는 ID가 하이픈 없이, Notion 응답에는 하이픈 있게 들어온다. 비교는 항상 wdb.notion_id로 한다.
    """
    roots = [r for i, r in enumerate(roots) if r and wdb.notion_id(r) not in {wdb.notion_id(x) for x in roots[:i]}]
    wanted = {wdb.notion_id(r) for r in roots}
    stats: dict[str, Any] = {"pages_checked": 0, "structure_changed": [], "sections_removed": [], "sections_added": [],
                             "judged": 0, "records_new": 0, "superseded": 0, "parts_judged": 0, "roots": {}}
    for page_id, old in [(p["page_id"], p) for p in wsections.pages(store, scope) if wdb.notion_id(p["root_id"]) not in wanted]:
        wsections.save_page(store, scope, page_id, old["root_id"], old["path"], old["last_edited"], old["fingerprint"], status="gone")
        stats["sections_removed"] += _remove_page_sections(store, scope, page_id, keep=set())
    for index, root in enumerate(roots):
        if index > 0 and wdb.notion_id(root) in _covered_ids(store, scope, other_than_root=root):
            _retire_root(store, scope, root, stats)
            stats["roots"][root] = "inside first root"
            continue
        try:
            one = sync(store, scope, root, client, classify, retire_other_roots=False, classify_parts=classify_parts,
                       rejudge_unknown=rejudge_unknown)
        except Exception as error:
            if index == 0:
                raise  # 팀 정보 페이지를 못 읽으면 실패로 알린다
            stats["roots"][root] = f"error: {error}"  # 회의록 페이지를 못 읽어도 팀 정보는 그대로 쓴다
            continue
        stats["roots"][root] = "synced"
        for key, value in one.items():
            if key in stats and isinstance(value, (int, list)):
                stats[key] += value
    return stats


def _sync_database_root(store: Path, scope: str, database_id: str, client: Client, classify: Classifier | None,
                        stats: dict[str, Any]) -> None:
    """루트가 페이지가 아니라 데이터베이스(예: 회의록 DB)일 때: DB 전체가 한 섹션이다."""
    meta = client.databases.retrieve(database_id)
    title = "".join(t.get("plain_text", "") for t in meta.get("title", [])) or "데이터베이스"
    stored = next((p for p in wsections.pages(store, scope) if p["page_id"] == database_id), None)
    fp = wdb.sha256(["database", database_id, title])
    if not stored or stored["fingerprint"] != fp:
        stats["structure_changed"].append(title)
        section = {"id": database_id, "page_id": database_id, "title": title, "path": title, "inherit_from": None,
                   "inherit_id": None, "layout": {"inline": False, "pages": [], "databases": [{"id": database_id, "title": title}],
                                                  "unreadable": []}}
        before = {s["id"] for s in wsections.sections(store, scope, database_id)}
        stats["judged"] += _judge_sections(store, scope, client, [section], [], title, classify)
        wsections.save_section(store, scope, section)
        stats["sections_added"] += [] if database_id in before else [title]
    wsections.save_page(store, scope, database_id, database_id, title, meta.get("last_edited_time"), fp)


def sync(store: Path, scope: str, root_id: str, client: Client, classify: Classifier | None = None,
         retire_other_roots: bool = True, classify_parts: wparts.PartClassifier | None = None,
         rejudge_unknown: bool = False) -> dict[str, Any]:
    """루트 하나: 구조 확인 → (바뀐 페이지만) 재스캔 → 지도대로 수집. 한 번 호출이 한 번의 접근이다.
    루트를 바꿨으면 옛 루트의 지도는 정리한다. 루트가 여러 개면 sync_roots를 쓴다."""
    stats: dict[str, Any] = {"pages_checked": 0, "structure_changed": [], "sections_added": [], "sections_removed": [],
                             "judged": 0, "records_new": 0, "superseded": 0}
    all_pages = {p["page_id"]: p for p in wsections.pages(store, scope)}
    same_root = wdb.notion_id(root_id)
    if retire_other_roots:  # 루트 하나로 부를 때: 루트가 바뀌었으면 옛 지도 정리(sync_roots는 직접 정리한다)
        for page_id, old in all_pages.items():
            if wdb.notion_id(old["root_id"]) != same_root:
                wsections.save_page(store, scope, page_id, old["root_id"], old["path"], old["last_edited"], old["fingerprint"], status="gone")
                stats["sections_removed"] += _remove_page_sections(store, scope, page_id, keep=set())
    known = {pid: p for pid, p in all_pages.items() if wdb.notion_id(p["root_id"]) == same_root}
    queue: list[tuple[str, str | None, int]] = [(root_id, None, 0)]
    queue += [(pid, p["path"], p["path"].count(" > ")) for pid, p in known.items() if pid != root_id]
    seen_pages: set[str] = set()
    dropped: set[str] = set()  # 상위 페이지 재스캔에서 빠진 하위 페이지(와 그 아래)
    fresh_blocks: dict[str, list[dict]] = {}

    while queue:
        page_id, path, depth = queue.pop(0)
        if page_id in seen_pages or page_id in dropped:
            continue
        seen_pages.add(page_id)
        stats["pages_checked"] += 1
        try:
            meta = client.pages.retrieve(page_id)
        except Exception:
            if page_id == root_id:
                _sync_database_root(store, scope, root_id, client, classify, stats)  # 루트가 데이터베이스(회의록 DB 등)
                continue
            wsections.save_page(store, scope, page_id, root_id, path or page_id, None, None, status="gone")
            stats["sections_removed"] += _remove_page_sections(store, scope, page_id, keep=set())
            continue
        path = path or _page_title(meta) or "팀 정보"
        edited = meta.get("last_edited_time")
        stored = known.get(page_id)
        if stored and stored["last_edited"] == edited:
            wsections.touch_page(store, scope, page_id)  # 확인한 시각만 남긴다
            continue  # 아무것도 안 바뀜: 블록 목록도 읽지 않는다
        blocks = top_blocks(client, page_id)
        fresh_blocks[page_id] = blocks
        fp = fingerprint(blocks)
        if not stored or stored["fingerprint"] != fp:
            stats["structure_changed"].append(path)
            found, subpages = scan_page(page_id, path, blocks, depth)
            if not found and not subpages and depth > 0:
                # 제목도 하위 항목도 없는 하위 페이지: 페이지 전체가 한 섹션
                found = [{"id": page_id, "page_id": page_id, "title": path.split(" > ")[-1], "path": path,
                          "inherit_from": None, "inherit_id": None, "layout": {"inline": False, "pages": [{"id": page_id, "title": path}],
                                                           "databases": [], "unreadable": []}}]
            stats["judged"] += _judge_sections(store, scope, client, found, blocks, path, classify, rejudge_unknown)
            before = {s["id"] for s in wsections.sections(store, scope, page_id)}
            for section in found:
                wsections.save_section(store, scope, section)
            stats["sections_added"] += [s["path"] for s in found if s["id"] not in before]
            stats["sections_removed"] += _remove_page_sections(store, scope, page_id, keep={s["id"] for s in found})
            for sub_id, sub_title in subpages:
                queue.append((sub_id, f"{path} > {sub_title}", depth + 1))
            # 이 페이지의 직접 하위 페이지 중 빠진 것과, 그 아래 페이지들을 정리한다.
            kept = {sub_id for sub_id, _ in subpages}
            for other_id, other in known.items():
                rest = other["path"][len(path) + 3:] if other["path"].startswith(f"{path} > ") else None
                if rest is not None and " > " not in rest and other_id not in kept:
                    dropped.add(other_id)
                    dropped.update(pid for pid, p in known.items() if p["path"].startswith(f"{other['path']} > "))
        wsections.save_page(store, scope, page_id, root_id, path, edited, fp)

    for page_id, stored in known.items():  # 상위 페이지에서 사라진 하위 페이지
        if page_id not in seen_pages or page_id in dropped:
            wsections.save_page(store, scope, page_id, root_id, stored["path"], stored["last_edited"], stored["fingerprint"], status="gone")
            stats["sections_removed"] += _remove_page_sections(store, scope, page_id, keep=set())

    mine = {p["page_id"] for p in wsections.pages(store, scope) if wdb.notion_id(p["root_id"]) == same_root}
    for section in wsections.sections(store, scope):
        hold = wsections.hold_for(section)
        if hold is False or section["page_id"] not in mine:  # 다른 루트의 섹션은 그 루트를 맞출 때 모은다
            continue
        new, superseded = _collect_section(store, scope, client, section, fresh_blocks, hold)
        stats["records_new"] += new
        stats["superseded"] += superseded
    # 새로 생기거나 바뀐 부분만 종류를 판정한다(LLM 없으면 다음 동기화로 미룬다). Notion은 부르지 않는다.
    stats["parts_judged"] = wparts.judge_pending(store, scope, classify_parts)
    return stats


def _remove_page_sections(store: Path, scope: str, page_id: str, keep: set[str]) -> list[str]:
    gone = [s for s in wsections.sections(store, scope, page_id) if s["id"] not in keep]
    wsections.deactivate(store, [s["id"] for s in gone])
    return [s["path"] for s in gone]


def _save(store: Path, scope: str, section: dict, unit_id: str, text: str, edited: str | None, hold: str | None) -> tuple[int, int]:
    if not text.strip():
        return 0, 0
    label = ROLE_LABELS.get(section["role"], section["role"])
    result = worklog.record(store, scope=scope, kind="notion_record", conversation_key=wsections.conversation_key(section["id"]),
                            locator=f"notion:{unit_id}", text=f"[{label}] {section['path']}\n{text}", sent_at=edited,
                            hold=hold, metadata={"section_id": section["id"], "role": section["role"]})
    return int(result["created"]), result.get("superseded", 0) if result["created"] else 0


def _row_title(row: dict) -> str:
    for prop in row.get("properties", {}).values():
        if prop.get("type") == "title":
            return "".join(t.get("plain_text", "") for t in prop.get("title", [])) or "(제목 없음)"
    return "(제목 없음)"


def _collect_parts(store: Path, scope: str, client: Client, section: dict, unit_id: str, title: str,
                   edited: str | None) -> str | None:
    """하위 페이지·DB 행 본문을 md로 한 번에 받아 부분으로 저장하고, 원문 저장용 글을 돌려준다(spec P1-1·P1-2).
    md를 못 받으면 None(호출부가 예전 블록 읽기로 대신한다). 블록 목록과 어긋나면 블록 ID 없이 저장한다."""
    page = notion_md.fetch_markdown(client, unit_id)
    if page.md is None:
        return None
    try:
        blocks = _list_child_blocks(unit_id, client)
    except Exception:
        blocks = None
    built = wparts.build_unit_parts(unit_id, f"{section['path']} > {title}", page.md, blocks)
    wparts.save_unit_parts(store, scope, "notion", unit_id, section["id"], built, last_edited=edited)
    metrics.count("parts", len(built))
    return wparts.md_to_text(page.md)


def _collect_section(store: Path, scope: str, client: Client, section: dict, fresh_blocks: dict[str, list[dict]],
                     hold: str | None) -> tuple[int, int]:
    new = superseded = 0
    layout = section["layout"]
    if layout.get("inline") and section["page_id"] in fresh_blocks:
        n, s = _save(store, scope, section, section["id"], _inline_text(client, fresh_blocks[section["page_id"]], section["id"]),
                     None, hold)
        new, superseded = new + n, superseded + s
    for page in layout.get("pages", []):
        try:
            edited = client.pages.retrieve(page["id"]).get("last_edited_time")
        except Exception:
            continue
        if edited and wsections.unit_edited(store, page["id"]) == edited:
            wsections.set_unit_parent(store, page["id"], page["id"])  # 출처 기록 전에 모은 것: 출처만 채운다
            if not wparts.has_parts(store, scope, page["id"]):
                _collect_parts(store, scope, client, section, page["id"], page["title"], edited)  # 부분 전에 모은 것: 부분만 채운다
            continue
        text = _collect_parts(store, scope, client, section, page["id"], page["title"], edited)
        if text is None:
            text = _fetch_notion_page_by_id(page["id"], client)
        n, s = _save(store, scope, section, page["id"], text, edited, hold)
        new, superseded = new + n, superseded + s
        wsections.save_unit(store, section["id"], page["id"], edited or "", parent_id=page["id"])
    # 출처(어느 DB의 행인지)를 기록하기 전에 모은 행이 있으면, 이번 한 번은 커서 없이 전체 행 목록을 받아 출처를 채운다.
    backfill = wsections.units_missing_parent(store, section["id"])
    for database in layout.get("databases", []):
        latest = section.get("cursor")
        for row in _query_rows(client, database["id"], None if backfill else section.get("cursor")):
            edited = row.get("last_edited_time")
            if edited and wsections.unit_edited(store, row["id"]) == edited:
                wsections.set_unit_parent(store, row["id"], database["id"])
                if not wparts.has_parts(store, scope, row["id"]):
                    _collect_parts(store, scope, client, section, row["id"], _row_title(row), edited)
                continue
            body = _collect_parts(store, scope, client, section, row["id"], _row_title(row), edited)
            if body is None:
                body = "\n".join(line for b in _list_child_blocks(row["id"], client) for line in _block_text(b, client))
            n, s = _save(store, scope, section, row["id"], "\n".join(filter(None, [_row_text(row), body])), edited, hold)
            new, superseded = new + n, superseded + s
            wsections.save_unit(store, section["id"], row["id"], edited or "", parent_id=database["id"])
            latest = max(filter(None, [latest, edited]), default=None)
        if latest != section.get("cursor"):
            wsections.set_cursor(store, section["id"], latest)
    return new, superseded
