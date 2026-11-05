"""근거와 방법론을 사용하는 팀 조율. 외부 API를 직접 호출하지 않는다."""
from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import Any

import prompts

from . import board, core, db, decision_state, leading, people
from .db import ContractError, ValidationError

METHODS = ("scrum-inspect-adapt", "raid", "product-discovery", "design-sprint", "spike")
METHOD_LABELS = {"scrum-inspect-adapt": "Scrum", "raid": "RAID", "product-discovery": "Product Discovery",
                 "design-sprint": "Design Sprint", "spike": "Spike"}
SECTIONS = (("now", "지금 함께 정할 것"), ("later", "나중에 정할 것"),
            ("cross_role", "역할 간 조율"), ("parallel", "병렬 진행"))
INPUT_BUDGET = 5000
FORCED = re.compile(r"반드시.{0,40}(?:해야|따라|필수)|무조건|강제(?:해야|한다)|유일한\s*방법")


def gather(store: Path, scope: str, issue_states: dict[int, str] | None = None,
           commit_touched: set[int] | None = None, question: str = "", repo: str | None = None) -> dict:
    """저장된 미정·관계·할 일·역할과 호출자가 확인한 GitHub 상태를 조립한다."""
    snapshot = board.snapshot(store, scope)
    items = []
    for item in snapshot["items"]:
        if item["deferred"]:
            continue
        if item["kind"] not in ("decision", "requirement", "open", "hypothesis", "task"):
            continue
        refs = [{"label": s["title"], "url": s.get("section_url") or s.get("url"),
                 "date": s.get("date")} for s in item["sources"][:3]]
        if not refs and item["quotes"]:
            refs = [{"label": "사람이 기록한 내용", "url": None, "date": None}]
        github = leading.github_state(item["github"], issue_states or {}, commit_touched or set())
        if github and repo and re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
            refs.append({"label": f"GitHub 이슈 #{github['number']}" + (" (추정 연결)" if github["linked"] == "inferred" else ""),
                         "url": f"https://github.com/{repo}/issues/{github['number']}", "date": None})
        links = [{"type": link["type"], "direction": link["direction"],
                  "item_id": link["other"]["id"], "title": link["other"]["title"]}
                 for link in item["links"] if link["type"] in
                 (core.LINK_AFFECTS, core.LINK_DERIVED_FROM, core.LINK_ALTERNATIVE_TO, core.LINK_RELATED_TO)]
        items.append({"id": item["id"], "kind": item["kind"], "title": item["title"],
                      "statement": item["statement"][:500], "status": item["status"],
                      "decision_state": decision_state.resolve(item["maturity"]), "assignee": item["assignee"],
                      "relations": links, "refs": refs, "quotes": item["quotes"][:2],
                      "github": github})
    items.sort(key=lambda i: (i["kind"] == "task", not bool(i["relations"]),
                              i["kind"] not in ("open", "hypothesis") and i["status"] != "needs_discussion"))
    primary = [i for i in items if i["kind"] != "task"][:16]
    primary_ids = {i["id"] for i in primary}
    linked_ids = {r["item_id"] for i in primary for r in i["relations"]}
    tasks = [i for i in items if i["kind"] == "task"]
    tasks.sort(key=lambda i: not (i["id"] in linked_ids or any(r["item_id"] in primary_ids for r in i["relations"])))
    # 예산 축소가 뒤에서부터 항목을 제거하므로 관련 작업을 앞쪽에 섞어 둔다.
    selected = []
    for n in range(max(len(primary), min(len(tasks), 8))):
        if n < len(primary):
            selected.append(primary[n])
        if n < min(len(tasks), 8):
            selected.append(tasks[n])
    selected_ids = {i["id"] for i in selected}
    selected += [i for i in items if i["id"] not in selected_ids][:24 - len(selected)]
    team = [{"name": m.get("display_name"), "role": m.get("role")} for m in people.team(store, scope)]
    return {"items": selected, "omitted_items": max(len(items) - len(selected), 0),
            "people": team, "stage": snapshot["stage"], "question": question}


def act(store: Path, scope: str, item_id: str, action: str, actor: str) -> dict:
    """명시적인 버튼 클릭에서만 기존 항목에 안건·보류 속성을 기록한다."""
    if action not in ("agenda", "defer", "resume"):
        raise ValidationError("unknown coordination action")
    db.initialize(store)
    with db.write(store) as c:
        row = board._live(c, scope, item_id)
        attrs = json.loads(row["attributes_json"])
        updated = dict(attrs)
        if action == "agenda":
            updated.update(agenda=True, deferred=False)
        else:
            updated["deferred"] = action == "defer"
        if updated != attrs:
            core._write_item(c, item_id, {"attributes_json": updated}, f"조율에서 {action} 선택", actor, db.now())
        return {"id": item_id, "title": row["title"], "action": action}


def item_title(store: Path, scope: str, item_id: str) -> str:
    db.initialize(store)
    with closing(db.connect(store)) as c:
        return board._live(c, scope, item_id)["title"]


def agendas(store: Path, scope: str) -> list[dict]:
    """다음 회의 안건만 표시하고 보류·삭제된 항목은 제외한다."""
    snapshot = board.snapshot(store, scope)
    return [{"id": i["id"], "title": i["title"]} for i in snapshot["items"] if i["agenda"] and not i["deferred"]]


def deferred(store: Path, scope: str) -> list[dict]:
    snapshot = board.snapshot(store, scope)
    return [{"id": i["id"], "title": i["title"]} for i in snapshot["items"] if i["deferred"]]


def record_decision(store: Path, scope: str, related_id: str, text: str, actor: str) -> dict:
    """사람이 직접 입력한 문장만 결정과 원문 근거로 한 트랜잭션에 저장한다."""
    statement = text.strip()
    if not statement or not actor.strip():
        raise ValidationError("결정 문장과 작성자가 필요해요")
    if len(statement) > 2000:
        raise ValidationError("결정 문장은 2000자 이하여야 해요")
    db.initialize(store)
    with db.write(store) as c:
        board._live(c, scope, related_id)
        ts, source_id = db.now(), core.new_id()
        item_id = core._create_item(c, scope, "decision",
                                    {"title": statement[:80], "statement": statement, "maturity": "agreed"},
                                    "조율에서 사람이 기록", actor, ts)
        metadata = {"scope": scope, "author": actor, "origin": "coordination"}
        c.execute("""INSERT INTO sources(id,kind,locator,captured_at,accessible_excerpt,content,content_hash,metadata_json,parse_status)
                     VALUES (?, 'teams_message', ?, ?, ?, ?, ?, ?, 'complete')""",
                  (source_id, f"coordination:{source_id}", ts, statement[:300], statement, db.sha256(statement),
                   db.to_json(metadata)))
        evidence_id = core.new_id()
        c.execute("INSERT INTO evidence(id,source_id,section_locator,excerpt,evidence_kind) VALUES (?,?,'human_input',?,'observed')",
                  (evidence_id, source_id, statement))
        c.execute("INSERT INTO object_evidence(object_type,object_id,evidence_id) VALUES ('work_item',?,?)", (item_id, evidence_id))
        row = c.execute("SELECT attributes_json FROM work_items WHERE id = ?", (item_id,)).fetchone()
        attrs = json.loads(row["attributes_json"])
        attrs["human_recorded"] = True
        core._write_item(c, item_id, {"attributes_json": attrs}, core.HUMAN_COORDINATION_PREFIX, actor, ts)
        core._link(c, item_id, related_id, core.LINK_RELATED_TO, None, ts)
    return {"id": item_id, "title": statement[:80], "statement": statement}


def select_methods(context: dict[str, Any]) -> list[str]:
    """저장된 신호로 방법론을 최대 두 개 고른다."""
    items = context.get("items", [])
    text = " ".join([str(context.get("question") or "")] +
                    [str(i.get("title") or "") + " " + str(i.get("statement") or "") for i in items]).lower()
    relations = {r.get("type") for i in items for r in i.get("relations", [])}
    if relations & {"AFFECTS", "RELATED_TO", "ALTERNATIVE_TO"} or any(x in text for x in ("의존", "충돌", "위험", "리스크")):
        return ["raid", "product-discovery"]
    if any(x in text for x in ("와이어프레임", "화면", "프로토타입", "디자인")):
        return ["design-sprint", "product-discovery"]
    if any(x in text for x in ("회고", "회의 주기", "스프린트")):
        return ["scrum-inspect-adapt", "raid"]
    if any(i.get("kind") in ("hypothesis", "open") for i in items):
        return ["product-discovery", "spike"]
    return ["product-discovery", "raid"]


def _payload(context: dict[str, Any], methods: list[str], first: dict | None) -> tuple[dict, dict]:
    """프롬프트와 선택한 문서 본문을 포함해 입력 예산을 지킨다."""
    index = prompts.load("methods/index").text
    references = [{"id": name, "text": prompts.load(f"methods/{name}").text} for name in methods]
    included = list(context.get("items", []))
    prompt_cost = core.estimate_tokens(prompts.load("coordination").text)
    while True:
        shown = dict(context, items=included,
                     omitted_items=int(context.get("omitted_items") or 0) + len(context.get("items", [])) - len(included))
        payload = {"context": shown, "method_index": index, "methods": references, "first": first}
        total = prompt_cost + core.estimate_tokens(json.dumps(payload, ensure_ascii=False))
        if total <= INPUT_BUDGET:
            without = dict(payload, method_index="", methods=[])
            base = prompt_cost + core.estimate_tokens(json.dumps(without, ensure_ascii=False))
            return payload, {"input_tokens": total, "without_methods_tokens": base,
                             "method_tokens": total - base, "method_count": len(references)}
        if not included:
            raise ContractError("coordination input exceeds token budget")
        included = included[:-1]


def validate(raw: Any, context: dict, methods: list[str], *, ignore_need: bool = False) -> dict:
    """입력에 존재하는 항목과 선택된 방법론만 참조하는 결과를 받는다."""
    if not isinstance(raw, dict):
        raise ContractError("CoordinationResult must be an object")
    required = [name for name, _ in SECTIONS] + ["compared_alternatives", "methods_used", "need_method"]
    if any(k not in raw for k in required):
        raise ContractError("CoordinationResult missing required fields")
    known = {i["id"] for i in context.get("items", [])}
    known_roles = {v for person in context.get("people", []) for v in
                   (person.get("name"), person.get("role"), people.ROLES.get(person.get("role"))) if v}
    known_roles.update(i["assignee"] for i in context.get("items", []) if isinstance(i.get("assignee"), str) and i["assignee"])
    result: dict[str, Any] = {}
    for key, _ in SECTIONS:
        entries = raw[key]
        if not isinstance(entries, list) or len(entries) > 10:
            raise ContractError(f"{key} must be a list of at most 10 entries")
        cleaned = []
        for entry in entries:
            if not isinstance(entry, dict) or entry.get("item_id") not in known:
                raise ContractError(f"{key} has unknown item")
            if not all(isinstance(entry.get(k), str) and entry[k].strip() for k in ("suggestion", "reason")):
                raise ContractError(f"{key} needs suggestion and reason")
            roles, evidence = entry.get("roles"), entry.get("evidence_ids")
            if not isinstance(roles, list) or not all(isinstance(r, str) and r in known_roles for r in roles):
                raise ContractError("roles must come from the visible team")
            if not isinstance(evidence, list) or not evidence or not all(isinstance(e, str) and e in known for e in evidence):
                raise ContractError("evidence_ids must identify input items")
            if FORCED.search(entry["suggestion"] + " " + entry["reason"]):
                raise ContractError("mandatory process is not allowed")
            cleaned.append({k: entry[k] for k in ("item_id", "suggestion", "reason", "roles", "evidence_ids")})
        result[key] = cleaned
    proposed_ids = [entry["item_id"] for key, _ in SECTIONS for entry in result[key]]
    if len(set(proposed_ids)) > 5 or len(proposed_ids) > 10:
        raise ContractError("at most five distinct items and ten proposals are allowed")
    for key, _ in SECTIONS:
        ids = [entry["item_id"] for entry in result[key]]
        if len(ids) != len(set(ids)):
            raise ContractError("the same item cannot repeat in one section")
    if {i["item_id"] for i in result["now"]} & {i["item_id"] for i in result["later"]}:
        raise ContractError("the same item cannot be both now and later")
    compared = raw["compared_alternatives"]
    if not isinstance(compared, list) or len(compared) > 6:
        raise ContractError("compared_alternatives must be a list")
    for entry in compared:
        if not isinstance(entry, dict) or not all(isinstance(entry.get(k), str) and entry[k].strip()
                                                  for k in ("option", "fits_when", "tradeoffs")):
            raise ContractError("alternative needs option, fits_when and tradeoffs")
        if FORCED.search(" ".join(entry[k] for k in ("option", "fits_when", "tradeoffs"))):
            raise ContractError("mandatory process is not allowed")
    used = raw["methods_used"]
    if not isinstance(used, list) or not all(isinstance(m, str) and m in methods for m in used):
        raise ContractError("methods_used must be included in this call")
    if (any(result[key] for key, _ in SECTIONS) or compared) and not used:
        raise ContractError("a proposal needs at least one referenced method")
    need = None if ignore_need else raw["need_method"]
    if need is not None and (not isinstance(need, str) or need not in METHODS):
        raise ContractError("unknown need_method")
    result.update(compared_alternatives=compared, methods_used=list(dict.fromkeys(used)), need_method=need)
    return result


def render(result: dict, context: dict) -> str:
    """제안을 출처와 함께 보여 준다. 생성만으로 저장하지 않는다."""
    items = {i["id"]: i for i in context.get("items", [])}
    lines = ["🧭 팀 조율 제안", "아래 내용은 검토할 제안이며 결정된 내용이 아니에요."]
    for key, title in SECTIONS:
        if result[key]:
            lines += ["", f"📌 {title}"]
        for entry in result[key]:
            lines.append(f"- {entry['suggestion']} — 이유: {entry['reason']}")
            if entry["roles"]:
                lines.append("  관련 역할: " + ", ".join(entry["roles"]))
            refs = []
            for item_id in entry["evidence_ids"]:
                item = items[item_id]
                for ref in item.get("refs", []):
                    refs.append(ref.get("label", "저장 기록") + (f" — {ref['url']}" if ref.get("url") else ""))
                if not item.get("refs"):
                    refs.append(item.get("title", "저장 기록") + " (원문 링크 미확인)")
            lines.append("  근거: " + "; ".join(dict.fromkeys(refs)))
    if result["compared_alternatives"]:
        lines += ["", "🔀 대안 비교"]
        for option in result["compared_alternatives"]:
            lines.append(f"- {option['option']}: 적합한 경우 {option['fits_when']} · 고려할 점 {option['tradeoffs']}")
    if result["methods_used"]:
        lines += ["", "📚 참고 방법론: " + ", ".join(METHOD_LABELS[m] for m in result["methods_used"])]
    omitted = context.get("omitted_items", 0)
    scope = f"🔎 조율 입력: 저장 항목 {len(items)}건"
    if omitted:
        scope += f", 입력 한도로 {omitted}건 제외"
    lines += ["", scope]
    return "\n".join(lines)


def propose(context: dict, call: Callable, *, question_path: bool = False, first: dict | None = None) -> dict:
    """메뉴는 최대 두 번, 질문 경로는 한 번 coordination을 호출한다."""
    methods = select_methods(context)
    calls, usages, budgets = 0, [], []
    while True:
        payload, budget = _payload(context, methods, first)
        calls += 1
        logging.getLogger("worklog").info("coordination request=%d path=%s methods=%s estimated_input=%d without_methods=%d",
                                          calls, "question" if question_path else "menu", methods,
                                          budget["input_tokens"], budget["without_methods_tokens"])
        raw, usage, _ = call("coordination", payload)
        usages.append(usage)
        budgets.append(budget)
        result = validate(raw, payload["context"], methods, ignore_need=question_path or calls == 2)
        need = result["need_method"]
        if question_path or calls >= 2 or need is None or need in methods:
            break
        methods = [methods[0], need]
    logging.getLogger("worklog").info("coordination calls=%d method_counts=%s estimated_input=%s without_methods=%s actual_prompt_tokens=%s",
                                      calls, [x["method_count"] for x in budgets],
                                      [x["input_tokens"] for x in budgets], [x["without_methods_tokens"] for x in budgets],
                                      [u.get("prompt_tokens") for u in usages])
    return {"result": result, "text": render(result, payload["context"]), "calls": calls, "usage": usages,
            "budgets": budgets, "context": payload["context"]}
