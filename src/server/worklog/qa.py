"""질문하기(Phase 5): 근거 조립, 호출 흐름, 답 검증·표시. 문서: docs/implementation/v2.1/plan.md 2.4, Phase 5.

원칙(조회 범위의 정직성):
- 이 모듈은 이미 저장된 지식(검색 색인)과 호출부가 넘긴 GitHub 사실만 본다. Notion·LLM·GitHub를 직접 부르지 않는다.
  해석하지 않은 Notion 문서는 질문 때 새로 탐색하지 않는다.
- 답에서 코드가 정하는 것: 조회 범위(`searched_scope`), 근거 링크, GitHub 상태, 명시/추정 표시, 성숙도. 모델의 값은 믿지 않고 입력에 있던 id만 받는다.
- GitHub는 이슈 종료·PR 병합·변경 파일 경로까지만 사실이다. "동작한다·검증됐다"는 말은 걸러내고 검증은 미확인으로 남긴다.
- 질문 한 건에 모델 호출은 최대 2회다(2차 결과의 need·intent는 무시, 3차 호출은 예외).
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import Any

import prompts

from . import board, coordination, core, db, decision_state, leading, parts, search
from .db import ContractError

MAX_CALLS = 2
MAX_NEED = 5
MAX_ITEMS = 8  # 모델에 보여 주는 항목 수의 상한
MAX_PRS = 3  # 변경 파일 경로를 조회하는 PR 수의 상한
MAX_FILES = 8  # PR 하나에서 보여 주는 변경 파일 경로 수
STATEMENT_CHARS = 300
QA_BUDGET_TOKENS = 5000  # plan 2.5: qa 입력 예산
INTENTS = ("answer", "coordination")
LINK_BASIS = ("explicit", "inferred")
NEED_TYPES = ("item_source", "github")
NEXT_TYPES = ("ask", "agenda")
REQUIRED_KEYS = ("intent", "answer", "findings", "work_status", "searched_scope", "unknowns", "need", "next")
FORBIDDEN = leading.FORBIDDEN
# PR의 변경 경로와 병합 상태만으로 기능 구현이나 코드의 실제 위치를 확인할 수 없다.
UNVERIFIED_IMPLEMENTATION = re.compile(
    r"(?:구현|개발)\s*(?:이|은|는)?\s*(?:됐|된|되었|되어|되는|되지|완료|했|한\s*것)|"
    r"(?:코드|구현).{0,80}(?:파일|경로).{0,30}(?:있|존재)|"
    r"(?:파일|경로).{0,40}(?:코드|구현).{0,20}(?:있|존재)"
)
# 질문이 구현·작업 상태에 관한 것이면 GitHub를 본다(코드가 정한다. 모델이 정하지 않는다)
GITHUB_HINT = re.compile(r"구현|개발|기능|완료|끝났|끝난|진행|이슈|풀\s*리퀘|(?<![a-z0-9_])PRs?(?![a-z0-9_])|코드|작업|배포|병합|머지|커밋|파일|브랜치|merge|commit", re.I)
ISSUE_MENTION = re.compile(r"#(\d+)")
REL = {"SUPERSEDES": ("이 항목이 대체함", "이 항목을 대체한 결정"), "RESOLVES": ("이 항목이 해결함", "이 미정을 해결한 항목"),
       "ALTERNATIVE_TO": ("이 항목은 대안(대상 결정)", "이 결정의 대안"), "DERIVED_FROM": ("이 항목이 나온 문서", "이 문서에서 나온 항목"),
       "AFFECTS": ("영향을 주는 대상", "영향을 받음"), "RELATED_TO": ("관련·충돌 가능", "관련·충돌 가능")}
PR_STATE = {"merged": "🔀 병합됨", "open": "🟢 열림", "closed": "⚪ 닫힘(병합 안 됨)", "unknown": "❔ 조회 범위 밖"}
NOT_VERIFIED = "※ GitHub에서 이슈가 닫히거나 PR이 병합된 것은 기능이 동작하는지 확인된 것이 아니에요. 동작 검증은 확인하지 않았어요."


class CallLimit(RuntimeError):
    """질문 한 건의 모델 호출 상한(2회)을 넘으려 했다."""


def wants_github(question: str) -> bool:
    return bool(GITHUB_HINT.search(question))


# ── 1. 축적된 지식 근거 조립 ────────────────────────────────

def gather(store: Path, scope: str, question: str) -> dict[str, Any]:
    """질문과 관련된 저장 항목(검색 색인)과 그 출처·관계·GitHub 연결. 코드가 실제로 본 범위(searched)도 함께 돌려준다."""
    db.initialize(store)
    found = search.query(store, scope, question, limit=MAX_ITEMS, max_tokens=4000)
    ids = [r["id"] for r in found["records"]]
    items: dict[str, dict[str, Any]] = {}
    with closing(db.connect(store)) as c:
        sources = board._sources(c, scope)
        links = board._links(c, scope)
        alias_of = {item_id: f"k{n + 1}" for n, item_id in enumerate(ids)}
        for item_id in ids:
            row = c.execute("SELECT * FROM work_items WHERE id = ?", (item_id,)).fetchone()
            if row is None:
                continue
            attributes = json.loads(row["attributes_json"])
            found_sources = [sources[p] for p in attributes.get("part_ids") or [] if p in sources] + board._human_sources(c, item_id)
            relations = []
            for link in links.get(item_id, []):
                names = REL.get(link["type"])
                if names is None:
                    continue
                other = link["other"]
                relations.append({"rel": names[0 if link["direction"] == "out" else 1], "title": other["title"],
                                  "id": alias_of.get(other["id"]), "status": other["status"]})
            maturity = decision_state.resolve(attributes.get("maturity"))["value"]
            items[alias_of[item_id]] = {
                "item_id": item_id, "kind": row["kind"], "status": row["status"], "title": row["title"],
                "statement": row["statement_md"], "maturity": None if maturity == "unknown" else maturity,
                "relations": relations[:6], "github": board._github(attributes), "quotes": board._quotes(c, item_id)[:3],
                "sources": sorted(found_sources, key=lambda s: s["date"] or "", reverse=True)[:3]}
        pages = [s for item in items.values() for s in item["sources"]]
        names = leading.label_pages(pages) if pages else {}
        for item in items.values():
            item["refs"] = [leading._ref(s, names.get(s["page_id"] or s["part_id"], s["title"]), section=True) for s in item["sources"]]
        indexed = found["searched"]["indexed"]
    selected = parts.selected_ids(store, scope)
    skipped = [p for p in parts.list_parts(store, scope) if p["kind"] == "document" and p["id"] not in selected]
    return {"question": question, "items": items, "searched": found["searched"], "indexed": indexed,
            "not_interpreted": {"areas": len(skipped), "pages": len({p["page_id"] for p in skipped})}}


# ── 2. GitHub 사실 ─────────────────────────────────────────

def linked_prs(ctx: dict[str, Any], prs: list[dict[str, Any]]) -> list[int]:
    """변경 파일 경로를 조회할 PR 번호: 항목에 연결된 PR + 연결된 이슈를 제목·본문에서 언급한 PR. 최대 MAX_PRS개."""
    wanted_issues = {int(r["number"]) for i in ctx["items"].values() for r in i["github"] if r["type"] == "issue"}
    numbers = [int(r["number"]) for i in ctx["items"].values() for r in i["github"] if r["type"] == "pr"]
    for pr in prs:
        mentioned = {int(m) for m in ISSUE_MENTION.findall(f"{pr.get('title') or ''} {pr.get('body') or ''}")}
        if mentioned & wanted_issues:
            numbers.append(int(pr["number"]))
    known = {int(p["number"]) for p in prs}
    return list(dict.fromkeys(n for n in numbers if n in known))[:MAX_PRS]


def github_pack(ctx: dict[str, Any], repo: str, issues: list[dict[str, Any]], prs: list[dict[str, Any]], commit_touched: set[int],
                files_by_pr: dict[int, list[str]], commits_checked: int = 0) -> dict[str, Any]:
    """질문과 관련된 GitHub 사실. 이슈 상태는 메뉴 2의 기존 분류 규칙(leading.github_state)을 그대로 쓴다.
    PR은 병합 여부와 변경 파일 경로까지만. facts[].basis: 항목에 적힌 연결의 basis(explicit|inferred), 또는 PR 제목·본문의 #번호(explicit)."""
    issue_states = {int(i["number"]): i["state"] for i in issues if "pull_request" not in i}
    pr_by_n = {int(p["number"]): p for p in prs}
    facts: dict[str, dict[str, Any]] = {}

    def fact(ref: str, kind: str, number: int) -> dict[str, Any]:
        return facts.setdefault(ref, {"ref": ref, "kind": kind, "number": number, "linked_items": [], "bases": set(), "files": [],
                                      "state_key": "unknown", "state": PR_STATE["unknown"]})

    for alias, item in ctx["items"].items():
        for ref in item["github"]:
            number = int(ref["number"])
            if ref["type"] == "issue":
                entry = fact(f"issue#{number}", "issue", number)
                state = leading.github_state([ref], issue_states, commit_touched)
                if state:
                    entry.update(state_key=state["key"], state=state["label"])
                else:
                    entry.update(state_key="unknown", state="❔ 조회 범위 밖(최근 이슈 목록에 없음)")
            else:
                entry = fact(f"pr#{number}", "pr", number)
                pr = pr_by_n.get(number)
                if pr:
                    key = "merged" if pr.get("merged_at") else ("open" if pr.get("state") == "open" else "closed")
                    entry.update(state_key=key, state=PR_STATE[key])
            entry["linked_items"].append(alias)
            entry["bases"].add(ref.get("basis", "explicit"))
    for pr in prs:  # 연결된 이슈를 제목·본문에서 언급한 PR
        number = int(pr["number"])
        mentioned = {int(m) for m in ISSUE_MENTION.findall(f"{pr.get('title') or ''} {pr.get('body') or ''}")}
        hit = [f for f in facts.values() if f["kind"] == "issue" and f["number"] in mentioned]
        if hit and f"pr#{number}" not in facts and number in linked_prs(ctx, prs):
            entry = fact(f"pr#{number}", "pr", number)
            key = "merged" if pr.get("merged_at") else ("open" if pr.get("state") == "open" else "closed")
            entry.update(state_key=key, state=PR_STATE[key])
            for h in hit:
                entry["bases"].update(h["bases"])
            for h in hit:
                entry["linked_items"] += [a for a in h["linked_items"] if a not in entry["linked_items"]]
    out = []
    for n, entry in enumerate(facts.values()):
        entry["id"] = f"g{n + 1}"
        entry["basis"] = "explicit" if "explicit" in entry["bases"] else "inferred"
        entry["files"] = files_by_pr.get(entry["number"], [])[:MAX_FILES] if entry["kind"] == "pr" else []
        entry["url"] = f"https://github.com/{repo}/{'issues' if entry['kind'] == 'issue' else 'pull'}/{entry['number']}"
        out.append(entry)
    return {"facts": out, "repo": repo, "issues": len(issue_states), "prs": len(prs), "commits": commits_checked,
            "files_prs": sorted(files_by_pr)}


# ── 3. 모델 입력·답 검증 ────────────────────────────────────

def build_payload(ctx: dict[str, Any], pack: dict[str, Any] | None, extra: list[dict[str, Any]] | None = None,
                  first: dict[str, Any] | None = None, budget_tokens: int = QA_BUDGET_TOKENS) -> dict[str, Any]:
    """모델에 보낼 입력. 항목은 짧은 id(k1…)로, GitHub 사실은 g1…로 보낸다. 예산을 넘으면 본문·항목·파일 경로 순으로 줄인다."""
    def make(limit_items: int, chars: int, files: int) -> dict[str, Any]:
        aliases = list(ctx["items"])[:limit_items]
        payload: dict[str, Any] = {
            "question": ctx["question"],
            "items": [{"id": a, "kind": ctx["items"][a]["kind"], "status": ctx["items"][a]["status"],
                       "maturity": ctx["items"][a]["maturity"], "title": ctx["items"][a]["title"],
                       "decision_state": decision_state.resolve(ctx["items"][a]["maturity"]),
                       "statement": leading._summary_of(ctx["items"][a]["statement"], chars),
                       "relations": [{"rel": r["rel"], "title": r["title"], "id": r["id"]} for r in ctx["items"][a]["relations"]],
                       "github": [{"ref": f"{r['type']}#{r['number']}", "linked": r.get("basis", "explicit")} for r in ctx["items"][a]["github"]],
                       "sources": [{"label": ref["label"], "date": ref["date"]} for ref in ctx["items"][a]["refs"]]} for a in aliases],
            "github": None}
        if pack is not None:
            payload["github"] = {"facts": [{"id": f["id"], "ref": f["ref"], "state": f["state"], "linked_items": f["linked_items"],
                                            "basis": f["basis"], "files": f["files"][:files]} for f in pack["facts"]]}
        if extra:
            payload["extra"] = extra
        if first:
            payload["first"] = first
        return payload

    prompt_tokens = core.estimate_tokens(prompts.load("qa").text)
    for limit_items, chars, files in ((MAX_ITEMS, STATEMENT_CHARS, MAX_FILES), (MAX_ITEMS, 160, 4), (5, 120, 3), (3, 80, 2), (2, 60, 0)):
        payload = make(limit_items, chars, files)
        if prompt_tokens + core.estimate_tokens(json.dumps(payload, ensure_ascii=False)) <= budget_tokens:
            return payload
    raise ContractError("question input exceeds the token budget")


def _strings(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ContractError(f"{label} must be a list of strings")
    return [v.strip() for v in value if v.strip()]


def validate_result(raw: Any, *, ignore_followup: bool = False) -> dict[str, Any]:
    """QaResult 계약 검사(plan 2.4). 필드 누락·잘못된 intent·need 6개 이상·형식 오류는 ContractError. 다시 묻지 않는다."""
    if not isinstance(raw, dict):
        raise ContractError("QaResult must be an object")
    missing = [k for k in REQUIRED_KEYS if k not in raw]
    if missing:
        raise ContractError(f"QaResult is missing {missing}")
    if not ignore_followup and raw["intent"] not in INTENTS:
        raise ContractError(f"intent must be one of {INTENTS}")
    answer = raw["answer"]
    if not isinstance(answer, str) or not answer.strip():
        raise ContractError("answer must be a non-empty string")
    findings = []
    if not isinstance(raw["findings"], list):
        raise ContractError("findings must be a list")
    for entry in raw["findings"]:
        if (not isinstance(entry, dict) or not isinstance(entry.get("claim"), str) or not entry["claim"].strip()
                or entry.get("link_basis") not in LINK_BASIS or not isinstance(entry.get("location", ""), str)):
            raise ContractError("findings entries need claim, evidence_ids, link_basis(explicit|inferred)")
        findings.append({"claim": entry["claim"].strip(), "evidence_ids": _strings(entry.get("evidence_ids"), "evidence_ids"),
                         "link_basis": entry["link_basis"]})
    status = []
    if not isinstance(raw["work_status"], list):
        raise ContractError("work_status must be a list")
    for entry in raw["work_status"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("ref"), str):
            raise ContractError("work_status entries need ref")
        status.append(entry["ref"].strip())
    need = [] if ignore_followup else raw["need"]
    if not isinstance(need, list) or len(need) > MAX_NEED:
        raise ContractError(f"need must be a list of at most {MAX_NEED}")
    for entry in need:
        if not isinstance(entry, dict) or entry.get("type") not in NEED_TYPES or not isinstance(entry.get("id"), str):
            raise ContractError("need entries need type(item_source|github) and id")
    nxt = raw["next"]
    if not isinstance(nxt, list):
        raise ContractError("next must be a list")
    for entry in nxt:
        if not isinstance(entry, dict) or entry.get("type") not in NEXT_TYPES or not isinstance(entry.get("text"), str):
            raise ContractError("next entries need type(ask|agenda) and text")
    return {"intent": "answer" if ignore_followup else raw["intent"], "answer": answer.strip(), "findings": findings, "work_status": status,
            "unknowns": _strings(raw["unknowns"], "unknowns"), "need": [dict(n) for n in need],
            "next": [{"type": n["type"], "text": n["text"].strip()[:80]} for n in nxt if n["text"].strip()][:2]}


def validate_need(need: list[dict[str, str]], ctx: dict[str, Any], pack: dict[str, Any] | None) -> tuple[list[dict[str, Any]], list[str]]:
    """1차 답의 need 검증(plan 5-1). 이미 해석된 항목의 근거 인용과, 이미 조회한 GitHub 사실만 허용한다(최대 5개).
    알 수 없는 id·조회하지 않은 GitHub 항목은 허용하지 않고 "확인하지 않음"으로 기록한다."""
    facts = {f["ref"]: f for f in (pack["facts"] if pack else [])}
    granted: list[dict[str, Any]] = []
    unknown: list[str] = []
    for entry in need[:MAX_NEED]:
        if entry["type"] == "item_source":
            item = ctx["items"].get(entry["id"])
            if item is None:
                unknown.append(f"{entry['id']}: 저장된 지식 항목이 아니라서 확인하지 않았어요")
            elif not item["quotes"]:
                unknown.append(f"{item['title']}: 근거 원문 구절이 저장돼 있지 않아요")
            else:
                granted.append({"type": "item_source", "id": entry["id"], "title": item["title"], "quotes": item["quotes"],
                                "sources": [{"label": r["label"], "date": r["date"]} for r in item["refs"]]})
        else:
            fact = facts.get(entry["id"])
            if fact is None or fact["state_key"] == "unknown":
                unknown.append(f"{entry['id']}: GitHub 조회 범위 밖이라 확인하지 않았어요")
            else:
                granted.append({"type": "github", "id": entry["id"], "state": fact["state"], "files": fact["files"],
                                "basis": fact["basis"]})
    return granted, unknown


# ── 4. 호출 흐름 ───────────────────────────────────────────

def ask(ctx: dict[str, Any], github: dict[str, Any], call: Callable[[str, dict[str, Any]], tuple[dict, dict, str]],
        tab_link: Callable[[str], str] | None = None, coord_context: Callable[[], dict] | None = None) -> dict[str, Any]:
    """질문 한 건(plan 2.4). 모델 호출은 최대 2회이고 3회째는 CallLimit. github = {"status", "repo", "pack", ...}(호출부가 실제로 조회한 것).
    1차 → intent=coordination이면 조율 1회 / need가 있으면 검증해 2차 qa 1회 / 아니면 1차로 끝.
    계약 위반이나 호출 실패는 재시도 없이 "답을 만들지 못함 + 조회 범위"로 끝낸다."""
    calls: list[dict[str, Any]] = []
    pack = github.get("pack") if github.get("status") == "ok" else None

    def guarded(payload: dict[str, Any], *, ignore_followup: bool = False) -> dict[str, Any]:
        if len(calls) >= MAX_CALLS:
            raise CallLimit("질문 한 건의 모델 호출은 2회까지예요")
        calls.append({"usage": {}, "model": None})
        data, usage, model = call("qa", payload)
        calls[-1] = {"usage": usage, "model": model}
        return validate_result(data, ignore_followup=ignore_followup)

    def shown(payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
        item_ids = {item["id"] for item in payload["items"]}
        item_ids.update(x["id"] for x in payload.get("extra", []) if x.get("type") == "item_source")
        visible_ctx = dict(ctx, items={key: value for key, value in ctx["items"].items() if key in item_ids})
        if pack is None:
            return visible_ctx, None
        fact_ids = {fact["id"] for fact in (payload.get("github") or {}).get("facts", [])}
        return visible_ctx, dict(pack, facts=[f for f in pack["facts"] if f["id"] in fact_ids])

    try:
        payload = build_payload(ctx, pack)
        first = guarded(payload)
        visible_ctx, visible_pack = shown(payload)
    except CallLimit:
        raise
    except Exception as error:
        return _failure(ctx, github, calls, error)
    result, unknowns, note = first, list(first["unknowns"]), None
    if first["intent"] == "coordination":
        try:
            if coord_context:
                context = coord_context()
            else:
                context = {"question": ctx["question"], "people": [], "stage": None,
                           "items": [{"id": alias, "kind": item["kind"], "status": item["status"],
                                      "title": item["title"], "statement": item["statement"],
                                      "relations": item["relations"], "refs": item["refs"]}
                                     for alias, item in visible_ctx["items"].items()]}
            context["question"] = ctx["question"]

            def guarded_coord(name: str, payload: dict):
                if len(calls) >= MAX_CALLS:
                    raise CallLimit("질문 한 건의 모델 호출은 2회까지예요")
                calls.append({"usage": {}, "model": None})
                data, usage, model = call(name, payload)
                calls[-1] = {"usage": usage, "model": model}
                return data, usage, model

            visible_ids = {i["id"] for i in context["items"]}
            prior_findings = []
            for finding in first["findings"]:
                evidence_ids = []
                for alias in finding["evidence_ids"]:
                    target = alias if alias in visible_ids else visible_ctx["items"].get(alias, {}).get("item_id")
                    if target in visible_ids:
                        evidence_ids.append(target)
                prior_findings.append(dict(finding, evidence_ids=evidence_ids))
            proposal = coordination.propose(context, guarded_coord, question_path=True,
                                             first={"answer": first["answer"], "findings": prior_findings})
        except CallLimit:
            raise
        except Exception as error:
            return _failure(ctx, github, calls, error)
        visible_github = dict(github, pack=visible_pack) if visible_pack is not None else github
        rendered = _render(first, unknowns, None, visible_ctx, visible_github, calls, tab_link)
        has_proposal = bool(proposal["result"]["compared_alternatives"] or
                            any(proposal["result"][key] for key, _ in coordination.SECTIONS))
        if has_proposal:
            rendered["text"] = rendered["text"].replace("저장된 근거로 질문에 대한 답을 확인하지 못했어요.",
                                                        "팀이 함께 검토할 내용을 정리했어요.", 1)
        rendered["text"] += "\n\n" + proposal["text"]
        rendered["coordination"] = proposal
        return rendered
    elif first["need"]:
        granted, rejected = validate_need(first["need"], visible_ctx, visible_pack)
        unknowns += rejected
        if granted:
            try:
                payload = build_payload(visible_ctx, visible_pack, extra=granted, first={
                    "answer": first["answer"], "claims": [f["claim"] for f in first["findings"]]})
                second = guarded(payload, ignore_followup=True)
                visible_ctx, visible_pack = shown(payload)
            except CallLimit:
                raise
            except Exception as error:
                return _failure(ctx, github, calls, error)
            result = second  # 2차 결과의 need·intent는 무시한다
            unknowns += [u for u in second["unknowns"] if u not in unknowns]
        # 통과한 근거가 없으면 2차를 부르지 않는다(붙일 것이 없다)
    visible_github = dict(github, pack=visible_pack) if visible_pack is not None else github
    return _render(result, unknowns, note, visible_ctx, visible_github, calls, tab_link)


def _sentences_without_claims(text: str) -> str:
    """답에서 "검증 완료" 같은 말이 든 문장을 뺀다(GitHub 종료·병합은 기능 검증이 아니다)."""
    kept = [s for s in re.split(r"(?<=[.!?。])\s+|\n+", text) if s.strip() and not _unsafe_claim(s)]
    return " ".join(kept).strip()


def _unsafe_claim(text: str) -> bool:
    return bool(FORBIDDEN.search(text) or UNVERIFIED_IMPLEMENTATION.search(text))


def scope_lines(ctx: dict[str, Any], github: dict[str, Any]) -> list[str]:
    """답의 조회 범위. 코드가 실제로 본 것만 쓴다."""
    s = ctx["searched"]
    lines = [f"저장된 지식 {ctx['indexed']}건 중 질문 단어로 {s['candidates']}건을 찾아 {len(ctx['items'])}건을 봤어요"]
    skipped = ctx["not_interpreted"]
    if skipped["areas"]:
        lines.append(f"해석 대상이 아니었던 문서 영역 {skipped['areas']}개(페이지 {skipped['pages']}개)는 확인하지 않았어요")
    lines.append("Notion은 질문하는 동안 새로 읽지 않았어요")
    status = github.get("status")
    if status == "ok":
        pack = github["pack"]
        line = f"GitHub {pack['repo']}: 이슈·PR 목록(이슈 {pack['issues']}건, PR {pack['prs']}건)과 최근 커밋 {pack['commits']}건을 봤어요"
        if pack["files_prs"]:
            line += f", PR {len(pack['files_prs'])}건의 변경 파일 경로를 봤어요"
        lines += [line, "코드 내용·테스트·동작은 확인하지 않았어요"]
    else:
        lines.append({"skipped": "GitHub는 보지 않았어요(질문이 구현·작업 상태에 관한 것이 아니에요)",
                      "no_repo": "GitHub 저장소가 설정돼 있지 않아 보지 못했어요",
                      "failed": "GitHub 조회에 실패해서 보지 못했어요"}.get(status or "", "GitHub는 보지 않았어요"))
    return lines


def _failure(ctx: dict[str, Any], github: dict[str, Any], calls: list[dict[str, Any]], error: Exception) -> dict[str, Any]:
    reason = "모델 답이 약속한 형식이 아니었어요" if isinstance(error, ContractError) else "모델을 부르지 못했어요"
    text = "\n".join(["😥 답을 만들지 못했어요 — " + reason, "", "🔍 조회 범위", *[f"- {line}" for line in scope_lines(ctx, github)]])
    return {"status": "failed", "text": text, "calls": len(calls), "usage": [c["usage"] for c in calls], "buttons": [], "view_item": None,
            "links": [], "scope": scope_lines(ctx, github), "error": f"{type(error).__name__}: {error}"}


def _render(result: dict[str, Any], unknowns: list[str], note: str | None, ctx: dict[str, Any], github: dict[str, Any],
            calls: list[dict[str, Any]], tab_link: Callable[[str], str] | None) -> dict[str, Any]:
    pack = github.get("pack") if github.get("status") == "ok" else None
    facts = {f["id"]: f for f in (pack["facts"] if pack else [])}
    numbering: dict[str, int] = {}
    links: list[dict[str, str]] = []
    first_item: list[str | None] = [None]

    def cite_item(alias: str) -> list[int]:
        out = []
        for ref in ctx["items"][alias]["refs"][:2]:
            url = ref["url"]
            kind = "notion"
            if (not url and ref.get("part_kind") == "human" and tab_link
                    and ctx["items"][alias]["status"] not in board.HIDDEN):
                url = tab_link(ctx["items"][alias]["item_id"])
                ref = dict(ref, url=url)
                kind = "human"
            if not url:
                continue
            key = url
            if key not in numbering:
                numbering[key] = len(numbering) + 1
                links.append({"n": numbering[key], "text": leading.ref_text(ref), "url": url, "kind": kind})
            out.append(numbering[key])
        if out and first_item[0] is None and ctx["items"][alias]["status"] not in board.HIDDEN:
            first_item[0] = ctx["items"][alias]["item_id"]
        return out

    def cite_fact(fact: dict[str, Any]) -> int:
        key = fact["url"]
        if key not in numbering:
            numbering[key] = len(numbering) + 1
            links.append({"n": numbering[key], "text": f"GitHub {fact['ref']}", "url": fact["url"], "kind": "github"})
        return numbering[key]

    lines: list[str] = []
    shown_facts: list[dict[str, Any]] = []
    decision_items: dict[str, dict[str, Any]] = {}
    claim_lines, dropped = [], 0
    for finding in result["findings"]:
        marks, basis = [], finding["link_basis"]
        for evidence in finding["evidence_ids"]:
            if evidence in ctx["items"]:
                marks += cite_item(evidence)
            elif evidence in facts and facts[evidence]["state_key"] != "unknown":
                fact = facts[evidence]
                marks.append(cite_fact(fact))
                shown_facts.append(fact)
                if fact["basis"] == "inferred":
                    basis = "inferred"
        if not marks or _unsafe_claim(finding["claim"]):
            dropped += 1  # 입력에 있던 근거가 하나도 없는 주장은 보이지 않는다
            continue
        suffix = " (추정 연결)" if basis == "inferred" else ""
        claim_lines.append(f"- {finding['claim']}{suffix} {''.join(f'[{m}]' for m in dict.fromkeys(marks))}")
        for evidence in finding["evidence_ids"]:
            item = ctx["items"].get(evidence)
            if item and item["kind"] in ("decision", "requirement", "hypothesis", "open") and any(r["url"] for r in item["refs"][:2]):
                decision_items[evidence] = item
    if claim_lines:
        lines.append(_sentences_without_claims(result["answer"]) or "확인한 내용을 정리했어요.")
        lines += ["", "📌 확인한 내용", *claim_lines]
    else:
        lines.append("저장된 근거로 질문에 대한 답을 확인하지 못했어요.")
    if decision_items:
        lines += ["", "📋 의사결정 상태 (저장 기록)"]
        for alias, item in decision_items.items():
            state = decision_state.resolve(item.get("maturity"))
            marks = "".join(f"[{n}]" for n in cite_item(alias))
            lines.append(f"- [{state['label']}] {item['title']} {marks}")
    wanted = [facts[r] for r in (_ref_to_id(ref, facts) for ref in result["work_status"]) if r]
    shown = list({f["id"]: f for f in [*wanted, *shown_facts]}.values()) or (list(facts.values())[:4] if facts else [])
    if shown:
        lines += ["", "🔨 작업 상태 (GitHub)"]
        for fact in shown[:6]:
            number = cite_fact(fact) if fact["state_key"] != "unknown" else None
            kind = "이슈" if fact["kind"] == "issue" else "PR"
            text = f"- {kind} #{fact['number']}: {fact['state']} ({'명시 연결' if fact['basis'] == 'explicit' else '추정 연결'})"
            if number is not None:
                text += f" [{number}]"
            items = [ctx["items"][a]["title"] for a in fact["linked_items"] if a in ctx["items"]][:2]
            if items:
                text += " · 관련: " + ", ".join(items)
            if fact["files"]:
                text += " · 변경 파일: " + ", ".join(fact["files"][:4]) + (f" 외 {len(fact['files']) - 4}건" if len(fact["files"]) > 4 else "")
            lines.append(text)
        lines.append(NOT_VERIFIED)
    unknown_lines = list(unknowns)
    if dropped:
        unknown_lines.append(f"근거를 찾지 못한 주장 {dropped}건은 뺐어요")
    if shown and not any("검증" in u or "동작" in u for u in unknown_lines):
        unknown_lines.append("기능이 실제로 동작하는지는 확인하지 않았어요")
    if unknown_lines:
        lines += ["", "❓ 확인하지 못한 것", *[f"- {u}" for u in unknown_lines]]
    scope = scope_lines(ctx, github)
    lines += ["", "🔍 조회 범위", *[f"- {line}" for line in scope]]
    if note:
        lines += ["", note]
    if links:
        lines += ["", "📎 근거 링크"]
        for link in links:
            lines.append(f"[{link['n']}] {link['text']}" + (f" — {link['url']}" if link["kind"] == "github" else ""))
    first = first_item[0]
    buttons = []
    asks = [n for n in result["next"] if n["type"] == "ask"]
    agendas = [n for n in result["next"] if n["type"] == "agenda"]
    topic = (asks or agendas or [{"text": f"질문 확인: {ctx['question'][:60]}"}])[0]["text"]
    buttons.append({"title": "👤 담당자에게 확인 요청 안건으로", "data": {"action": "qa_agenda", "mode": "ask", "text": (asks or [{"text": topic}])[0]["text"]}})
    buttons.append({"title": "📅 다음 회의 안건으로", "data": {"action": "qa_agenda", "mode": "meeting", "text": (agendas or [{"text": topic}])[0]["text"]}})
    return {"status": "ok", "text": "\n".join(lines), "calls": len(calls), "usage": [c["usage"] for c in calls], "buttons": buttons,
            "view_item": first, "links": links, "scope": scope, "result": result, "unknowns": unknown_lines}


def _ref_to_id(ref: str, facts: dict[str, dict[str, Any]]) -> str | None:
    for fact_id, fact in facts.items():
        if fact["ref"] == ref or fact_id == ref:
            return fact_id
    return None


# ── 5. 다음 행동: 안건 항목 ─────────────────────────────────

def create_agenda(store: Path, scope: str, text: str, mode: str, actor: str, question: str = "") -> dict[str, Any]:
    """[담당자에게 확인 요청 안건으로] / [다음 회의 안건으로]를 사람이 눌렀을 때만 안건 항목(kind=open, attributes.agenda=true)을 만든다.
    출처 원문이 없는 사람 입력 항목이라 maturity는 두지 않는다. 같은 제목의 살아 있는 미정이 있으면 ConflictError."""
    title = " ".join(text.split())[:80]
    if not title:
        raise db.ValidationError("안건 내용이 비었어요")
    if mode not in ("ask", "meeting"):
        raise db.ValidationError("안건 종류가 올바르지 않아요")
    db.initialize(store)
    with db.write(store) as c:
        statement = f"질문에서 나온 안건: {question[:200]}" if question else "질문에서 나온 안건"
        item_id = core._create_item(c, scope, "open", {"title": title, "statement": statement}, "질문하기에서 안건 등록", actor, db.now())
        row = c.execute("SELECT attributes_json FROM work_items WHERE id = ?", (item_id,)).fetchone()
        attributes = json.loads(row["attributes_json"])
        attributes.update(agenda=True, agenda_mode="ask_owner" if mode == "ask" else "next_meeting", agenda_by=actor)
        core._write_item(c, item_id, {"attributes_json": attributes}, "안건 표시", actor, db.now())
    return {"id": item_id, "title": title, "mode": mode}
