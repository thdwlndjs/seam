"""프로젝트 리딩(메뉴 1·2): 입력 조립, 모델 답 검증, 근거 표시. 문서: docs/implementation/v2.1/plan.md Phase 4.

원칙: 모델이 쓴 위치·이름·성숙도·GitHub 상태를 그대로 믿지 않는다.
- 구역(결정됨 / 진행 중 / 불명확), 성숙도 표시, GitHub 상태, 근거(출처 이름·날짜·링크)는 코드가 저장본에서 정한다.
- 모델은 어떤 항목을 먼저 보일지(순서)와 한 줄 설명, 개요 문단만 낸다. 항목 id·근거 id가 입력에 없으면 그 줄은 버린다.
- 근거를 하나도 찾지 못한 항목은 보이지 않고, 몇 건 뺐는지만 밝힌다.
- 출처 이름은 페이지 제목이고(경로의 마지막 마디), 같은 이름이 둘 이상이면 윗 경로 한 마디, 그래도 같으면 ID 꼬리로 구분한다.

이 모듈은 Notion·LLM·GitHub를 부르지 않는다(호출부가 결과를 넘긴다).
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import Any

from . import board, core, db, decision_state, sections
from .db import ContractError

DOC_SUMMARY_CHARS = 500  # 문서 하나의 요약 상한(최근 문서가 예산 밖으로 밀리지 않게 문서마다 자른다)
DOCS_BUDGET_TOKENS = 4000  # 메뉴 1 입력 중 산출물 문서 몫(plan Phase 4)
TEAM_INFO_CHARS = 1500  # 메뉴 1 입력 중 팀 소개 원문 몫
BRIEFING_BUDGET_TOKENS = 4000  # 메뉴 2 입력 상한
MAX_PER_SECTION = 10  # 구역마다 보이는 항목 수의 상한
RECENT_MEETINGS = 3  # "최근 회의" 판단에 쓰는 회의 페이지 수
NOTE_CHARS = 120
EVIDENCE_PER_LINE = 2  # 한 줄에 풀어 쓰는 근거 수(나머지는 "외 n건")
FORBIDDEN = re.compile(r"검증\s*(완료|됨|했|된)|동작\s*(확인|검증)|테스트\s*통과")  # GitHub 완료·이슈 종료는 기능 검증이 아니다
MATURITY_LABEL = decision_state.MATURITY_LABELS
GITHUB_LABEL = {"done": "✅ 이슈 종료", "in_progress": "🔨 진행 중(커밋 있음)", "not_started": "📋 이슈만 등록", "unlinked": "🆕 이슈 미등록"}
SECTIONS = (("decided", "📌 결정됨"), ("in_progress", "🔨 진행 중"), ("unclear", "❓ 불명확"))
ITEM_KINDS = ("decision", "requirement", "open", "hypothesis", "task")


# ── 출처 이름 ─────────────────────────────────────────────

def _day(iso: str | None) -> str:
    return (iso or "")[:10].replace("-", ".")


def label_pages(pages: list[dict[str, Any]]) -> dict[str, str]:
    """페이지 ID → 사람이 구분할 수 있는 이름. 페이지 제목이 같으면 윗 경로 한 마디를 붙이고, 그래도 같으면 ID 꼬리를 붙인다."""
    unique: dict[str, dict[str, Any]] = {}
    for page in pages:
        unique.setdefault(page["page_id"] or page["part_id"], page)
    labels = {key: page["title"] for key, page in unique.items()}

    def duplicated() -> set[str]:
        seen: dict[str, list[str]] = {}
        for key, label in labels.items():
            seen.setdefault(label, []).append(key)
        return {key for keys in seen.values() if len(keys) > 1 for key in keys}

    for key in duplicated():
        segments = (unique[key].get("page_path") or "").split(" > ")
        if len(segments) >= 2:
            labels[key] = f"{segments[-2]} > {segments[-1]}"
    for key in duplicated():
        labels[key] = f"{labels[key]} #{str(key)[-4:]}"
    return labels


def _ref(source: dict[str, Any], label: str, section: bool = False) -> dict[str, Any]:
    """출처 하나. section=True면 근거가 있는 부분(블록)으로 바로 가는 링크와 부분 제목을 쓴다(항목의 근거). 아니면 페이지 링크."""
    anchored = section and source.get("section_url") and "#" in source["section_url"]
    return {"page_id": source["page_id"], "label": label, "date": source["date"],
            "url": source["section_url"] if anchored else source["url"], "section": source.get("section") if anchored else None,
            "part_kind": source["part_kind"]}


def ref_text(ref: dict[str, Any]) -> str:
    """근거 한 줄: 이름(› 부분 제목)(날짜) — 링크."""
    name = ref["label"] + (f" › {ref['section']}" if ref.get("section") else "")
    when = f"({_day(ref['date'])})" if ref["date"] else ""
    return f"{name}{when}" + (f" — {ref['url']}" if ref["url"] else "")


# ── 메뉴 1: 산출물 문서 + 팀 정보 ──────────────────────────────

def _summary_of(statement: str, limit: int) -> str:
    """산출물 본문(요약 Markdown)에서 원문 링크 줄을 빼고 한 덩어리로."""
    lines = [line.strip("- ").strip() for line in statement.splitlines() if line.strip() and not line.startswith("원문:")]
    text = " ".join(lines)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _artifact_rows(c: Any, scope: str) -> list[dict[str, Any]]:
    sources = board._sources(c, scope)
    links = board._links(c, scope)
    out = []
    for row in c.execute(f"SELECT * FROM work_items WHERE scope = ? AND kind = 'artifact' AND status NOT IN ({','.join('?' * len(board.HIDDEN))})",
                         (scope, *board.HIDDEN)):
        item = board._artifact(c, row, sources, links)
        out.append(item)
    out.sort(key=lambda a: (a["date"] or "", a["updated_at"] or ""), reverse=True)
    return out


def project_docs(store: Path, scope: str, budget_tokens: int = DOCS_BUDGET_TOKENS, summary_chars: int = DOC_SUMMARY_CHARS) -> dict[str, Any]:
    """메뉴 1에 넣을 산출물 문서 요약. 최근 문서부터 담고, 문서마다 요약을 잘라서 최신 문서가 예산 밖으로 밀리지 않게 한다.
    {"docs": [{id, title, type, date, maturity, summary}], "refs": {id: 출처}, "omitted": 못 담은 문서 수}."""
    db.initialize(store)
    with closing(db.connect(store)) as c:
        artifacts = _artifact_rows(c, scope)
    pages = [dict(s, part_id=s["part_id"]) for a in artifacts for s in a["sources"][:1]]
    names = label_pages(pages) if pages else {}
    docs: list[dict[str, Any]] = []
    refs: dict[str, dict[str, Any]] = {}
    used = 0
    for artifact in artifacts:
        source = artifact["sources"][0] if artifact["sources"] else None
        if source is None:
            continue  # 출처를 못 찾은 문서는 근거 없이 쓰지 않는다
        label = names.get(source["page_id"] or source["part_id"], source["title"])
        entry = {"id": f"s{len(docs) + 1}", "title": label, "type": artifact.get("artifact_type"), "date": artifact["date"] or None,
                 "maturity": artifact.get("maturity"), "summary": _summary_of(artifact["statement"], summary_chars)}
        cost = core.estimate_tokens(json.dumps(entry, ensure_ascii=False))
        if docs and used + cost > budget_tokens:
            break
        docs.append(entry)
        refs[entry["id"]] = _ref(source, label)
        used += cost
    return {"docs": docs, "refs": refs, "omitted": max(len(artifacts) - len(docs), 0)}


def project_input(store: Path, scope: str, team_text: str | None) -> dict[str, Any] | None:
    """메뉴 1 입력. 산출물 문서가 하나도 없으면 None(호출부가 예전 방식으로 팀 정보만 요약한다)."""
    found = project_docs(store, scope)
    if not found["docs"]:
        return None
    payload: dict[str, Any] = {"docs": found["docs"]}
    refs = dict(found["refs"])
    if team_text and team_text.strip():
        payload["team_info"] = {"id": "t", "text": team_text.strip()[:TEAM_INFO_CHARS]}
        refs["t"] = {"page_id": None, "label": "팀 정보 페이지", "date": None, "url": None, "part_kind": None}
    return {"payload": payload, "refs": refs, "omitted": found["omitted"]}


def render_project(answer: Any, refs: dict[str, dict[str, Any]]) -> str:
    """메뉴 1 답 검증과 표시. 문단마다 입력에 있는 출처 id가 하나 이상 있어야 하고(없으면 그 문단은 버린다), 출처 이름·날짜·링크는
    코드가 채운다. 쓸 수 있는 문단이 없으면 ContractError."""
    paragraphs = answer.get("paragraphs") if isinstance(answer, dict) else None
    if not isinstance(paragraphs, list):
        raise ContractError("paragraphs must be a list")
    kept: list[tuple[str, list[str]]] = []
    for entry in paragraphs:
        if not isinstance(entry, dict):
            continue
        text = str(entry.get("text") or "").strip()
        sources = [s for s in (entry.get("sources") or []) if isinstance(s, str) and s in refs]
        if not text or not sources or FORBIDDEN.search(text):
            continue
        kept.append((text, list(dict.fromkeys(sources))))
    if not kept:
        raise ContractError("no paragraph with a valid source")
    numbering: dict[str, int] = {}
    body = []
    for text, sources in kept:
        marks = "".join(f"[{numbering.setdefault(s, len(numbering) + 1)}]" for s in sources)
        body.append(f"{text} {marks}")
    lines = ["\n\n".join(body), "", "📎 출처"]
    for source_id, number in numbering.items():
        ref = refs[source_id]
        when = f"({_day(ref['date'])})" if ref["date"] else ""
        lines.append(f"[{number}] {ref['label']}{when}" + (f" — {ref['url']}" if ref["url"] else ""))
    return "\n".join(lines)


# ── 메뉴 2: 브리핑 ─────────────────────────────────────────

def github_state(refs: list[dict[str, Any]], issue_states: dict[int, str], commit_touched: set[int]) -> dict[str, Any] | None:
    """항목의 GitHub 연결로 본 작업 상태. 분류 규칙은 메뉴 2의 기존 분류(main._classify_tasks_by_github_state)와 같다:
    이슈 종료 → done / 열림+커밋 있음 → in_progress / 열림 → not_started. 연결이 하나도 없으면 None.
    linked: 맞는 연결 중 명시(explicit)가 있으면 explicit, 추정(inferred)뿐이면 inferred."""
    matched = [r for r in refs if r.get("type") == "issue" and int(r["number"]) in issue_states]
    if not matched:
        return None
    explicit = [r for r in matched if r.get("basis", "explicit") == "explicit"]
    chosen = (explicit or matched)[0]
    number = int(chosen["number"])
    if issue_states[number] == "closed":
        key = "done"
    elif number in commit_touched:
        key = "in_progress"
    else:
        key = "not_started"
    return {"key": key, "label": GITHUB_LABEL[key], "number": number, "linked": "explicit" if explicit else "inferred"}


def _section_of(item: dict[str, Any]) -> str:
    if item["kind"] == "task":  # 담당이 정해진 할 일은 진행 중(GitHub 상태를 같이 보인다), 담당 미정은 불명확
        return "in_progress" if item["assignee"] else "unclear"
    if item["status"] == "needs_discussion" or item["kind"] in ("open", "hypothesis"):
        return "unclear"
    if decision_state.resolve(item.get("maturity"))["value"] != "agreed":
        return "unclear"
    return "in_progress" if item.get("github") else "decided"


def briefing_items(store: Path, scope: str, issue_states: dict[int, str], commit_touched: set[int],
                   task_states: dict[str, str] | None = None) -> dict[str, Any]:
    """메뉴 2 입력. 저장된 항목에서 구역·성숙도·GitHub 상태·근거를 코드가 정한다. task_states: 할 일 항목 ID → 기존 분류 키
    (done|in_progress|not_started|unlinked). 근거를 못 찾은 항목은 넣지 않고 excluded로 센다."""
    db.initialize(store)
    with closing(db.connect(store)) as c:
        sources = board._sources(c, scope)
        links = board._links(c, scope)
        rows = c.execute(f"""SELECT * FROM work_items WHERE scope = ? AND kind IN ({','.join('?' * len(ITEM_KINDS))})
                             AND status NOT IN ({','.join('?' * len(board.HIDDEN))}) ORDER BY updated_at DESC, rowid DESC""",
                         (scope, *ITEM_KINDS, *board.HIDDEN)).fetchall()
        items: list[dict[str, Any]] = []
        excluded = 0
        for row in rows:
            attributes = json.loads(row["attributes_json"])
            found = [sources[p] for p in attributes.get("part_ids") or [] if p in sources]
            if not found:
                excluded += 1
                continue
            mine = links.get(row["id"], [])
            github_refs = board._github(attributes)
            state = github_state(github_refs, issue_states, commit_touched)
            if row["kind"] == "task" and attributes.get("assignee"):
                key = (task_states or {}).get(row["id"]) or (state["key"] if state else "unlinked")
                state = {"key": key, "label": GITHUB_LABEL[key], "number": state["number"] if state else None,
                         "linked": state["linked"] if state else "explicit"}
            maturity = decision_state.resolve(attributes.get("maturity"))["value"]
            items.append({
                "id": row["id"], "kind": row["kind"], "status": row["status"], "title": row["title"],
                "statement": row["statement_md"], "assignee": attributes.get("assignee"),
                "maturity": None if maturity == "unknown" else maturity, "github": state,
                "alternatives": sum(1 for link in mine if link["type"] == core.LINK_ALTERNATIVE_TO and link["direction"] == "in"
                                    and link["other"]["status"] not in board.HIDDEN),
                "sources": sorted(found, key=lambda s: s["date"] or "", reverse=True)})
    meeting_pages: dict[str, dict[str, Any]] = {}
    for source in sources.values():
        if source["part_kind"] == "meeting" and source["page_id"]:
            meeting_pages.setdefault(source["page_id"], source)
    recent = sorted(meeting_pages.values(), key=lambda s: s["date"] or "", reverse=True)[:RECENT_MEETINGS]
    recent_ids = {s["page_id"] for s in recent}
    cited = [s for item in items for s in item["sources"]] + recent
    names = label_pages(cited) if cited else {}
    for item in items:
        item["recent"] = any(s["page_id"] in recent_ids for s in item["sources"])
        item["refs"] = _item_refs(item["sources"], names)
        item["section"] = _section_of(item)
    order = {"agreed": 0, "discussed": 1, "recommended": 2, "draft": 3, None: 4}
    by_section: dict[str, list[dict[str, Any]]] = {key: [] for key, _ in SECTIONS}
    for item in items:
        by_section[item["section"]].append(item)
    for key, group in by_section.items():
        group.sort(key=lambda i: (not i["recent"], order[i["maturity"]]))
        by_section[key] = group[:MAX_PER_SECTION]
    aliased = {}
    for group in by_section.values():
        for item in group:
            aliased[f"i{len(aliased) + 1}"] = item
    meetings = {f"m{n + 1}": dict(_ref(s, names.get(s["page_id"], s["title"]))) for n, s in enumerate(recent)}
    return {"aliased": aliased, "by_section": by_section, "meetings": meetings, "excluded": excluded}


def _item_refs(found: list[dict[str, Any]], names: dict[str, str]) -> list[dict[str, Any]]:
    seen: dict[str, dict[str, Any]] = {}
    for source in found:
        key = source["page_id"] or source["part_id"]
        ref = _ref(source, names.get(key, source["title"]), section=True)
        seen.setdefault(ref["url"] or key, ref)  # 같은 페이지라도 부분이 다르면 따로 센다(링크가 다르다)
    return list(seen.values())


def briefing_payload(built: dict[str, Any], docs: list[dict[str, Any]]) -> dict[str, Any]:
    """모델에 보낼 입력. 항목은 짧은 별칭 id(i1…)로 보내고 구역은 코드가 정한 대로 알린다. 본문은 줄여서 보낸다."""
    alias_of = {id(item): alias for alias, item in built["aliased"].items()}
    items = []
    for alias, item in built["aliased"].items():
        entry = {"id": alias, "kind": item["kind"], "maturity": item["maturity"], "title": item["title"],
                 "decision_state": decision_state.resolve(item["maturity"]),
                 "statement": _summary_of(item["statement"], 160), "alternatives": item["alternatives"], "recent": item["recent"]}
        if item["status"] == "needs_discussion":
            entry["needs_discussion"] = True
        if item["github"]:
            entry["github"] = {"label": item["github"]["label"], "linked": item["github"]["linked"]}
        items.append(entry)
    return {"items": items,
            "sections": {key: [alias_of[id(i)] for i in group] for key, group in built["by_section"].items()},
            "recent_meetings": [{"id": mid, "title": ref["label"], "date": ref["date"]} for mid, ref in built["meetings"].items()],
            "docs": [{"title": d["title"], "type": d["type"], "date": d["date"], "summary": d["summary"][:200]} for d in docs[:3]]}


def fit_payload(built: dict[str, Any], docs: list[dict[str, Any]], budget_tokens: int = BRIEFING_BUDGET_TOKENS) -> dict[str, Any]:
    """입력이 예산을 넘으면 문서 요약 → 구역별 항목 수 순으로 줄인다."""
    payload = briefing_payload(built, docs)
    while core.estimate_tokens(json.dumps(payload, ensure_ascii=False)) > budget_tokens:
        if payload["docs"]:
            payload["docs"] = payload["docs"][:-1]
            continue
        longest = max(built["by_section"], key=lambda k: len(built["by_section"][k]))
        if not built["by_section"][longest]:
            break
        dropped = built["by_section"][longest].pop()
        built["aliased"] = {a: i for a, i in built["aliased"].items() if i is not dropped}
        payload = briefing_payload(built, docs)
    return payload


def render_briefing(answer: Any, built: dict[str, Any], tab_url: str | None = None) -> str:
    """메뉴 2 답 검증과 표시. 구역·성숙도·GitHub 상태·근거는 저장본에서 코드가 쓴다. 모델의 한 줄 설명은 항목 id가 입력에 있고
    "검증 완료" 같은 말이 없을 때만 붙고, 모델이 내용을 안 줘도 줄은 그대로 보인다. lines가 목록이 아니면 ContractError."""
    if not isinstance(answer, dict) or not isinstance(answer.get("lines"), list):
        raise ContractError("lines must be a list")
    notes: dict[str, str] = {}
    order: dict[str, int] = {}
    for entry in answer["lines"]:
        alias = entry.get("item") if isinstance(entry, dict) else None
        if alias not in built["aliased"] or alias in order:
            continue  # 입력에 없는 id는 버린다
        order[alias] = len(order)
        note = str(entry.get("note") or "").strip()
        title = built["aliased"][alias]["title"]
        repeats = db.normalize_text(title) in db.normalize_text(note) and len(note) < len(title) + 16  # 제목을 되풀이할 뿐인 설명은 쓰지 않는다
        if note and not FORBIDDEN.search(note) and not repeats:
            notes[alias] = note[:NOTE_CHARS]
    alias_of = {id(item): alias for alias, item in built["aliased"].items()}
    numbering: dict[str, int] = {}
    footer: list[str] = []

    def cite(ref: dict[str, Any]) -> int:
        key = ref["url"] or ref["page_id"] or ref["label"]
        if key not in numbering:
            numbering[key] = len(numbering) + 1
            footer.append(f"[{numbering[key]}] {ref_text(ref)}")
        return numbering[key]

    out: list[str] = []
    for key, title in SECTIONS:
        group = sorted(built["by_section"][key], key=lambda i: order.get(alias_of.get(id(i)), len(order) + 1000))
        group = [i for i in group if id(i) in alias_of]
        if not group:
            continue
        out += ["", title]
        for item in group:
            out.append(_line(item, notes.get(alias_of[id(item)]), cite))
    overview = answer.get("overview")
    head: list[str] = []
    if isinstance(overview, dict) and str(overview.get("text") or "").strip() and not FORBIDDEN.search(str(overview["text"])):
        evidence = [e for e in (overview.get("evidence") or []) if isinstance(e, str) and (e in built["aliased"] or e in built["meetings"])]
        if evidence:  # 근거 id가 하나도 입력에 없으면 개요는 보이지 않는다
            refs = []
            for e in evidence:
                refs += built["aliased"][e]["refs"][:1] if e in built["aliased"] else [built["meetings"][e]]
            marks = "".join(f"[{cite(r)}]" for r in {r["url"] or r["page_id"] or r["label"]: r for r in refs}.values())
            head = [f"{str(overview['text']).strip()} {marks}"]
    if not out and not head:
        raise ContractError("nothing to show")
    tail: list[str] = []
    if footer:
        tail += ["", "📎 근거", *footer]
    if built["excluded"]:
        tail += ["", f"(근거 출처를 찾지 못해 뺀 항목 {built['excluded']}건)"]
    if tab_url:
        tail += ["", f"🗂 출처·이력과 고치기는 구조 보기에서: {tab_url}"]
    return "\n".join([*head, *out, *tail]).strip()


def _line(item: dict[str, Any], note: str | None, cite: Callable[[dict[str, Any]], int]) -> str:
    parts = []
    state = decision_state.resolve(item.get("maturity"))
    if state["value"] != "unknown" or item["kind"] in ("decision", "requirement"):
        parts.append(f"[{state['label']}]")
    if item["status"] == "needs_discussion":
        parts.append("[확인 필요]")
    text = f"- {' '.join(parts)} {item['title']}".replace("-  ", "- ")
    if item["kind"] == "task" and item["assignee"]:
        text += f" ({item['assignee']})"
    if note:
        text += f" — {note}"
    extra = []
    if item["alternatives"]:
        extra.append(f"대안 {item['alternatives']}건")
    github = item.get("github")
    if github:
        extra.append(github["label"] + ("" if github["linked"] == "explicit" else " (추정 연결)"))
    elif item["kind"] == "task" and item["assignee"]:
        extra.append(GITHUB_LABEL["unlinked"])
    refs = item["refs"]
    marks = "".join(f"[{cite(r)}]" for r in refs[:EVIDENCE_PER_LINE])
    if len(refs) > EVIDENCE_PER_LINE:
        marks += f" 외 {len(refs) - EVIDENCE_PER_LINE}건"
    return text + (" · " + " · ".join(extra) if extra else "") + f" · 근거 {marks}"
