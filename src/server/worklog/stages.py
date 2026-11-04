"""단계 길잡이: 게이트 충족 판정, 논의 중인 단계, 단계 제안과 답. 문서: docs/v1.1/spec.md P2-3, P2-4.

- 단계는 권한자의 답(answer)으로만 바뀐다(A28). 판정은 제안만 만든다.
- 게이트 기본값은 stage_gates의 scope '*' 행(마이그레이션 8). 팀이 고치면 그 팀의 행이 생긴다.
- 사람이 정한 게이트 상태(later·skipped)는 stage_gates에, 충족 여부는 판정 결과(team_stages.assessment_json)에 둔다.
- 같은 질문(종류·단계·게이트·그때의 충족 근거)은 prompts_asked에 답이 있으면 다시 하지 않는다(A16).
- 기본값·판정 규칙은 정답지가 없어 가정이다(docs/v1.1/plan.md "가정으로 진행하는 것의 수정 절차").
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import Any

from . import apply, db, parts

log = logging.getLogger("stage_judge")

STAGES = [("1-1", "문제 탐색"), ("1-2", "방향 확정"), ("1-3", "구체화·타당성"), ("2-1", "VOC 설계"), ("2-2", "VOC 수집"),
          ("2-3", "VOC 분석·반영"), ("3-1", "백로그"), ("3-2", "설계"), ("3-3", "스프린트 반복"), ("3-4", "릴리스·검증")]
STAGE_NAMES = dict(STAGES)
ORDER = [s for s, _ in STAGES]
CODE_GATES = {"voc_collected", "backlog", "milestones"}  # 코드로 판정(LLM 안 씀)
MAX_ITEMS = 80  # 판정에 보내는 최근 항목 수
MAX_DOCS = 10  # 함께 보내는 문서 부분 수(기획서 등). 한 줄 설명 + 앞부분
DOC_HEAD_CHARS = 200
STATEMENT_CHARS = 120
BUDGET_TOKENS = 5000  # [가정] 판정 한 번 입력 상한(추정). Groq 무료 요청 한도(413)를 넘지 않게
KIND_ORDER = {"decision": 0, "open": 1, "task": 2, "insight": 3, "document": 4, "needs_discussion": 5, "feedback": 6}
StageJudge = Callable[[dict], dict]


PHASES = [("1", "문제 정의", ("1-1", "1-2", "1-3")), ("2", "VOC", ("2-1", "2-2", "2-3")),
          ("3", "개발", ("3-1", "3-2", "3-3", "3-4"))]  # 상위 단계(카드 표시줄용). 세부 단계는 STAGES


def phase_of(stage: str) -> str:
    return next(no for no, _, members in PHASES if stage in members)


def phases(stage: str) -> list[dict[str, str]]:
    """상위 단계 표시줄: stage가 속한 상위 단계가 current, 앞은 done, 뒤는 todo."""
    at = int(phase_of(stage))
    return [{"no": no, "name": name, "state": "done" if int(no) < at else "current" if int(no) == at else "todo"}
            for no, name, _ in PHASES]


def stepper(highlight: str) -> list[dict[str, str]]:
    """10단계 전체 목록(탭의 단계 상세 화면용): highlight 앞은 done, 그 단계는 current, 뒤는 todo."""
    at = ORDER.index(highlight)
    return [{"code": code, "name": name, "state": "done" if n < at else "current" if n == at else "todo"}
            for n, (code, name) in enumerate(STAGES)]


def label(stage: str | None) -> str:
    return f"{stage} {STAGE_NAMES.get(stage or '', '')}".strip() if stage else "시작 전"


def _row(store: Path, scope: str) -> dict[str, Any] | None:
    db.initialize(store)
    with closing(db.connect(store)) as c:
        row = c.execute("SELECT * FROM team_stages WHERE scope = ?", (scope,)).fetchone()
    if row is None:
        return None
    out = dict(row)
    out["history"] = json.loads(out.pop("history_json") or "[]")
    out["assessment"] = json.loads(out.pop("assessment_json") or "null")
    return out


def current(store: Path, scope: str) -> str | None:
    """승인된 현재 단계. 아직 정하지 않았으면 None(첫 제안에서 권한자가 정한다)."""
    row = _row(store, scope)
    return row["stage"] if row and row["stage"] != "-" else None


def assessment(store: Path, scope: str) -> dict[str, Any] | None:
    row = _row(store, scope)
    return row["assessment"] if row else None


def gates(store: Path, scope: str, stage: str | None = None) -> list[dict[str, Any]]:
    """그 팀의 게이트(공통 기본값 + 팀이 고친 것). removed는 뺀다. 단계 순서대로."""
    db.initialize(store)
    with closing(db.connect(store)) as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM stage_gates WHERE scope IN ('*', ?)", (scope,))]
    merged: dict[tuple[str, str], dict] = {}
    for row in sorted(rows, key=lambda r: r["scope"] != "*"):  # 공통 먼저, 팀 것이 덮는다
        merged[(row["stage"], row["gate_id"])] = row
    out = [g for g in merged.values() if g["state"] != "removed" and (stage is None or g["stage"] == stage)]
    return sorted(out, key=lambda g: (ORDER.index(g["stage"]) if g["stage"] in ORDER else 99, g["gate_id"]))


def set_gate_state(store: Path, scope: str, stage: str, gate_id: str, state: str, actor: str) -> None:
    template = {(g["stage"], g["gate_id"]): g for g in gates(store, scope)}.get((stage, gate_id))
    if template is None:
        raise db.NotFoundError(f"게이트 {stage} {gate_id} 없음")
    with db.write(store) as c:
        c.execute("""INSERT INTO stage_gates(scope, stage, gate_id, text, state, edited_by, updated_at)
                     VALUES (?, ?, ?, ?, ?, ?, ?)
                     ON CONFLICT(scope, stage, gate_id) DO UPDATE SET state = excluded.state,
                         edited_by = excluded.edited_by, updated_at = excluded.updated_at""",
                  (scope, stage, gate_id, template["text"], state, actor, db.now()))


def edit_gate(store: Path, scope: str, stage: str, gate_id: str, text: str | None, actor: str) -> None:
    """권한자가 팀 게이트를 더하거나(새 gate_id) 고치거나(text) 뺀다(text=None → removed)."""
    with db.write(store) as c:
        c.execute("""INSERT INTO stage_gates(scope, stage, gate_id, text, state, edited_by, updated_at)
                     VALUES (?, ?, ?, ?, ?, ?, ?)
                     ON CONFLICT(scope, stage, gate_id) DO UPDATE SET text = excluded.text, state = excluded.state,
                         edited_by = excluded.edited_by, updated_at = excluded.updated_at""",
                  (scope, stage, gate_id, text or "(뺌)", "open" if text else "removed", actor, db.now()))


# ── 판정 ────────────────────────────────────────────────

def snapshot(store: Path, scope: str) -> tuple[list[dict[str, Any]], str]:
    """판정 입력: 반영된 항목(MAX_ITEMS개)과 문서 부분(기획서 등, 한 줄 설명과 앞부분)과 그 지문.
    문서는 회의록이 아니라 추출·반영하지 않지만, 주제 후보·현재 상태처럼 단계를 가르는 내용이 문서에 있다.
    문서 근거 ID는 'doc:<부분 ID>'."""
    live = apply.live_items(store, scope)
    items = [{"id": i["id"], "kind": i["kind"], "title": i["title"],
              "statement": (i.get("statement_md") or "")[:STATEMENT_CHARS]}
             for group in live.values() for i in group]
    with closing(db.connect(store)) as c:
        docs = [dict(r) for r in c.execute(
            """SELECT id, path, summary, md FROM source_parts WHERE scope = ? AND status = 'active' AND kind = 'document'
               ORDER BY chars DESC LIMIT ?""", (scope, MAX_DOCS))]
    items += [{"id": f"doc:{d['id']}", "kind": "document", "title": d["path"].rsplit(" > ", 1)[-1],
               "statement": " / ".join(filter(None, [d["summary"], parts.md_to_text(d["md"])[:DOC_HEAD_CHARS]]))}
              for d in docs]
    # 예산 안에서 단계를 가르는 종류부터(결정·미정·할 일·VOC·문서), 멘토 피드백은 남는 만큼
    items.sort(key=lambda i: (KIND_ORDER.get(i["kind"], 9), i["id"]))
    kept: list[dict] = []
    for item in items:
        if len(kept) >= MAX_ITEMS or parts.estimate_tokens(kept + [item]) > BUDGET_TOKENS:
            break
        kept.append(item)
    items = sorted(kept, key=lambda i: i["id"])
    return items, db.sha256(json.dumps(items, ensure_ascii=False, sort_keys=True))


def _code_gate(gate_id: str, items: list[dict], github: dict) -> dict[str, Any]:
    if gate_id == "voc_collected":
        found = [i["id"] for i in items if i["kind"] == "insight"]
        return {"met": bool(found), "evidence": found[:5], "by": "code"}
    if gate_id == "backlog":
        return {"met": github.get("open_issues", 0) > 0, "evidence": ["github:open_issues"] if github.get("open_issues") else [],
                "by": "code"}
    met = github.get("milestones", 0) > 0 and github.get("closed_issues", 0) > 0
    return {"met": met, "evidence": ["github:milestones"] if met else [], "by": "code"}


def judge(store: Path, scope: str, items: list[dict], github: dict, llm: StageJudge | None) -> dict[str, Any]:
    """게이트별 충족과 논의 중인 단계. 의미 판정은 LLM 한 번(대기열 순위 3). 근거 항목 ID가 없는 참은 받지 않는다."""
    all_gates = gates(store, scope)
    ids = {i["id"] for i in items}
    semantic = [g for g in all_gates if g["gate_id"] not in CODE_GATES]
    answer: dict[str, Any] = {}
    if llm is not None and items and semantic:
        answer = llm({"stages": [{"stage": s, "name": n} for s, n in STAGES],
                      "gates": [{"stage": g["stage"], "gate_id": g["gate_id"], "text": g["text"]} for g in semantic],
                      "items": items}) or {}
    raw = answer.get("gates") or {}
    result: dict[str, dict] = {}
    for gate in all_gates:
        if gate["gate_id"] in CODE_GATES:
            result[gate["gate_id"]] = _code_gate(gate["gate_id"], items, github)
            continue
        found = raw.get(gate["gate_id"]) or {}
        evidence = [e for e in found.get("evidence") or [] if e in ids]
        result[gate["gate_id"]] = {"met": bool(found.get("met")) and bool(evidence), "evidence": evidence, "by": "llm"}
    discussing = answer.get("discussing") or {}
    d_stage = discussing.get("stage") if discussing.get("stage") in ORDER else None
    d_evidence = [e for e in discussing.get("evidence") or [] if e in ids]
    overturned = answer.get("overturned") or None
    if overturned and not [e for e in overturned.get("evidence") or [] if e in ids]:
        overturned = None
    out = {"gates": result, "discussing": {"stage": d_stage if d_evidence else None, "evidence": d_evidence},
           "overturned": overturned, "judged": llm is not None and bool(items)}
    log.info("stage_judge scope=%s items=%s result=%s", scope, json.dumps(sorted(ids)),
             json.dumps({"gates": {k: [v["met"], v["evidence"]] for k, v in result.items()},
                         "discussing": out["discussing"], "overturned": bool(overturned)}, ensure_ascii=False))
    return out


def assess(store: Path, scope: str, github: dict, llm: StageJudge | None, force: bool = False) -> dict[str, Any] | None:
    """반영된 항목이 바뀌었을 때만 다시 판정해 저장한다. 판정 결과(바뀌지 않았으면 None)."""
    items, digest = snapshot(store, scope)
    row = _row(store, scope)
    key = f"{digest}:{json.dumps(github, sort_keys=True)}"
    if row and row["assessed_hash"] == key and not force:
        return None
    found = judge(store, scope, items, github, llm)
    with db.write(store) as c:
        c.execute("""INSERT INTO team_stages(scope, stage, assessment_json, assessed_hash) VALUES (?, '-', ?, ?)
                     ON CONFLICT(scope) DO UPDATE SET assessment_json = excluded.assessment_json,
                         assessed_hash = excluded.assessed_hash""",
                  (scope, json.dumps(found, ensure_ascii=False), key if found["judged"] else None))
    return found


# ── 제안과 답 ───────────────────────────────────────────

def _met(found: dict | None, gate_id: str) -> bool:
    return bool(found and (found["gates"].get(gate_id) or {}).get("met"))


def _done(gate: dict, found: dict | None) -> bool:
    return gate["state"] in ("later", "skipped") or _met(found, gate["gate_id"])


def first_open_stage(store: Path, scope: str, found: dict | None = None) -> str:
    """게이트가 다 끝나지 않은 첫 단계(판정상 지금 있어야 할 곳)."""
    found = found if found is not None else assessment(store, scope)
    for stage in ORDER:
        if any(not _done(g, found) for g in gates(store, scope, stage)):
            return stage
    return ORDER[-1]


def _key(*parts: Any) -> str:
    return "|".join(str(p) for p in parts)


def _evidence_key(found: dict | None, gate_id: str) -> str:
    gate = (found or {}).get("gates", {}).get(gate_id) or {}
    return f"{'met' if gate.get('met') else 'open'}:{','.join(sorted(gate.get('evidence') or []))}"


def proposals(store: Path, scope: str) -> list[dict[str, Any]]:
    """지금 물어볼 단계 제안(답한 것은 뺀다). 각 {kind, key, text, answers: [(답, 버튼 글)]}. 판정 전이면 빈 목록."""
    found = assessment(store, scope)
    if not found or not found.get("judged"):
        return []
    now = current(store, scope)
    target = first_open_stage(store, scope, found)
    out: list[dict[str, Any]] = []
    answered = _answered(store, scope)
    if now is None:
        # 처음 단계 설정은 질문 하나: 회의록이 앞서 있으면 그 단계, 아니면 게이트 기준 단계가 맞는지 묻는다.
        # [맞아요]는 그 단계로 저장하고 끝, [아니요]는 10단계 선택 카드로 이어진다(confirm_flow.answer_stage).
        ahead = found["discussing"].get("stage")
        missing = [g["text"] for g in gates(store, scope, target) if not _done(g, found)]
        follows_talk = bool(ahead and ORDER.index(ahead) > ORDER.index(target))
        candidate = ahead if follows_talk else target
        out.append({"kind": "stage_init", "key": _key("init", candidate, *sorted(missing)), "stage": candidate,
                    "ahead": candidate if follows_talk else None,
                    "text": f"회의록을 분석한 결과, 현재는 ‘{label(candidate)}’ 단계로 보여요. 맞나요?",
                    "answers": [("start_discussing" if follows_talk else "start_here", "맞아요"), ("not_ahead", "아니요")]})
    else:
        stage_gates = gates(store, scope, now)
        open_gates = [g for g in stage_gates if not _done(g, found)]
        if not open_gates and now != ORDER[-1]:
            out.append({"kind": "stage_advance", "key": _key(now, *[_evidence_key(found, g["gate_id"]) for g in stage_gates]),
                        "stage": now, "text": f"{label(now)}에서 정할 사안을 완료했어요. 다음 단계로 넘어갈까요?",
                        "answers": [("advance", "넘어갈게요"), ("stay", "아직이요")]})
        turned = found.get("overturned")
        if turned and ORDER.index(now) >= ORDER.index("2-1"):
            assumption = str(turned.get("assumption") or "가정")[:60]
            out.append({"kind": "pivot", "key": _key(now, assumption, *sorted(turned.get("evidence") or [])), "stage": now,
                        "text": f"'{assumption}'이 VOC에서 뒤집혔어요. 기획 단계(1-3)로 돌아갈까요?",
                        "answers": [("pivot", "돌아갈게요"), ("stay", "아니요")]})
    return [p for p in out if (p["kind"], p["key"]) not in answered]


def _answered(store: Path, scope: str) -> set[tuple[str, str]]:
    with closing(db.connect(store)) as c:
        return {(r["kind"], r["key"]) for r in c.execute(
            "SELECT kind, key FROM prompts_asked WHERE scope = ? AND answer IS NOT NULL", (scope,))}


def mark_asked(store: Path, scope: str, found: list[dict]) -> None:
    with db.write(store) as c:
        for p in found:
            c.execute("INSERT OR IGNORE INTO prompts_asked(scope, kind, key, asked_at) VALUES (?, ?, ?, ?)",
                      (scope, p["kind"], p["key"], db.now()))


def _set_stage(c: Any, scope: str, stage: str, actor: str, reason: str) -> None:
    row = c.execute("SELECT stage, history_json FROM team_stages WHERE scope = ?", (scope,)).fetchone()
    history = json.loads(row["history_json"]) if row else []
    history.append({"from": row["stage"] if row and row["stage"] != "-" else None, "to": stage, "by": actor,
                    "at": db.now(), "reason": reason})
    c.execute("""INSERT INTO team_stages(scope, stage, approved_by, approved_at, history_json) VALUES (?, ?, ?, ?, ?)
                 ON CONFLICT(scope) DO UPDATE SET stage = excluded.stage, approved_by = excluded.approved_by,
                     approved_at = excluded.approved_at, history_json = excluded.history_json""",
              (scope, stage, actor, db.now(), json.dumps(history, ensure_ascii=False)))


def with_ro(stage: str) -> str:
    """'2-3 VOC 분석·반영'으로 / '2-1 VOC 설계'로 — 받침에 맞는 조사(ㄹ 받침·받침 없음은 "로")를 붙인다."""
    name = label(stage)
    code = ord(name[-1]) - 0xAC00
    particle = "로" if 0 <= code < 11172 and code % 28 in (0, 8) else "으로"
    return f"'{name}'{particle}"


def answer(store: Path, scope: str, kind: str, key: str, reply: str, actor: str, stage_choice: str | None = None) -> str:
    """권한자의 답을 반영한다(권한 확인은 부르는 쪽). 단계는 여기서만 바뀐다(A28). 사람에게 보일 결과 한 줄.
    처음 단계 설정(stage_init): [맞아요]는 후보 단계를 저장, [아니요](not_ahead)는 저장 없이 선택 카드로 넘기고,
    선택 카드의 [확인](choose)은 고른 단계(stage_choice)를 저장한다."""
    pending = {(p["kind"], p["key"]): p for p in proposals(store, scope)}
    prompt = pending.get((kind, key))
    if prompt is None:
        raise db.ConflictError("이미 답했거나 상황이 바뀐 질문이에요")
    if reply not in {a for a, _ in prompt["answers"]} | ({"choose"} if kind == "stage_init" else set()):
        raise db.ValidationError(f"알 수 없는 답 {reply}")
    stage = prompt["stage"]
    message = "알겠어요."
    if reply == "not_ahead":  # 아직 정한 것이 없다: 질문은 그대로 두어, 선택 카드를 놓쳐도 다시 물을 수 있다
        return "알겠어요. 현재 진행 중인 단계를 선택해 주세요."
    if reply == "choose" and stage_choice not in ORDER:
        raise db.ValidationError("단계를 선택해 주세요.")
    with db.write(store) as c:
        if reply in ("start_here", "start_discussing"):
            _set_stage(c, scope, stage, actor, "첫 제안")
            message = f"현재 단계를 {with_ro(stage)} 설정했어요."
        elif reply == "choose":
            _set_stage(c, scope, stage_choice, actor, "첫 제안(직접 선택)")
            message = f"현재 단계를 {with_ro(stage_choice)} 설정했어요."
            reply = f"choose:{stage_choice}"
        elif reply == "advance":
            nxt = ORDER[ORDER.index(stage) + 1]
            _set_stage(c, scope, nxt, actor, "게이트 충족")
            message = f"다음 단계로 넘어갔어요: {label(nxt)}"
        elif reply == "pivot":
            _set_stage(c, scope, "1-3", actor, "VOC가 가정을 뒤집음")
            message = f"기획 단계로 돌아갔어요: {label('1-3')}"
        c.execute("""INSERT INTO prompts_asked(scope, kind, key, answer, answered_by, asked_at, answered_at)
                     VALUES (?, ?, ?, ?, ?, ?, ?)
                     ON CONFLICT(scope, kind, key) DO UPDATE SET answer = excluded.answer,
                         answered_by = excluded.answered_by, answered_at = excluded.answered_at""",
                  (scope, kind, key, reply, actor, db.now(), db.now()))
    return message


def board(store: Path, scope: str) -> dict[str, Any]:
    """현재 단계 표시용: 확정 단계, 화면에 보일 후보 단계(candidate), 이 단계에서 남은 게이트, 이전 단계에서 나중으로 미룬 게이트."""
    found = assessment(store, scope)
    now = current(store, scope)
    first = first_open_stage(store, scope, found) if found and found.get("judged") else None
    shown = now or first
    # 화면에 보일 "현재 단계": 확정 > 회의록이 논의 중인 단계(AI 추정) > 게이트 기준 첫 미완 단계. 메뉴 2·단계 카드·탭이 같은 값을 쓴다.
    ahead = ((found or {}).get("discussing") or {}).get("stage")
    candidate = now or (ahead if ahead in ORDER else None) or first
    doing = [g for g in gates(store, scope, shown) if not _done(g, found)] if shown else []
    earlier = [g for g in gates(store, scope) if shown and ORDER.index(g["stage"]) < ORDER.index(shown)
               and g["state"] == "later"]
    return {"stage": now, "shown": shown, "candidate": candidate, "approved": now is not None, "doing": doing, "earlier": earlier,
            "met": [g for g in gates(store, scope, shown) if _met(found, g["gate_id"])] if shown else []}
