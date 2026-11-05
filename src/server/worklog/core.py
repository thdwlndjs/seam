"""Team records (Teams messages, Notion rows) → Tasks and Decisions, with reviewed change candidates.

Flow:
1. `record` stores each raw record at once as an ordinary source (idempotent on kind+locator+content).
2. `due_batches` applies the analysis policy (idle / message count / token budget) per conversation.
3. `analyze_batch` asks the host's `extract` and `judge` callables (an LLM in practice) what the records
   say, compares every item with related existing Tasks/Decisions, and stores the judgment as a change
   candidate with its evidence and the compared ids. Nothing in the work state changes here.
4. `approve` / `reject` is the human step. Only an approved candidate changes a Task or Decision, and the
   change is written as a revision under the approver's name.

저장 구조(sources/evidence/revisions/runs)와 검색어 추출은 build-log에서 필요한 부분만 가져왔다(db.py, runs.py,
terms.py). 이 폴더만으로 동작하며 build-log 설치가 필요 없다.
"""
from __future__ import annotations

import json
import re
import sqlite3
import uuid
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from . import db, runs
from .db import ConflictError, ContractError, NotFoundError, ValidationError, normalize_text, to_json
from .terms import query_terms

RECORD_KINDS = {"teams_message", "notion_record"}
ITEM_KINDS = {"task", "decision"}
EXTRACT_KINDS = {"task", "decision", "progress", "opinion", "question"}
TASK_STATUSES = {"open", "in_progress", "done", "cancelled"}
DECISION_STATUSES = {"active", "superseded"}
CHANGE_TYPES = ("unchanged", "supplementary", "proposed", "conflicting", "superseding", "alternative", "resolving")
# v2.1 Phase 2: alternative = 기존 결정과 다른 가안·조언(기존 결정은 그대로, 새 항목을 ALTERNATIVE_TO로 둔다),
# resolving = 미정에 답하는 항목(미정은 resolved, RESOLVES). superseding은 결정끼리·요구사항끼리 같은 종류만.
CANDIDATE_STATUSES = {"pending", "approved", "rejected", "recorded"}
# A decision is a key decision when it touches one of these, or affects two or more tasks.
KEY_DECISION_AREAS = {"goal", "scope", "architecture", "budget", "schedule"}
LINK_AFFECTS, LINK_SUPERSEDES = "AFFECTS", "SUPERSEDES"
# v2.1 Phase 1: 지식 모델 확장(docs/implementation/v2.1/plan.md 2.3). 표 구조는 그대로이고 값만 늘었다.
LINK_DERIVED_FROM, LINK_RESOLVES, LINK_ALTERNATIVE_TO, LINK_RELATED_TO = "DERIVED_FROM", "RESOLVES", "ALTERNATIVE_TO", "RELATED_TO"
LINK_TYPES = {LINK_AFFECTS, LINK_SUPERSEDES, LINK_DERIVED_FROM, LINK_RESOLVES, LINK_ALTERNATIVE_TO, LINK_RELATED_TO}
KNOWLEDGE_KINDS = ("decision", "open", "task", "insight", "feedback", "artifact", "requirement", "hypothesis")
MATURITY = ("draft", "recommended", "discussed", "agreed")  # 가안 / 조언 / 논의 / 합의
# 살아 있는 항목이 아닌 상태. 같은 제목의 새 항목을 막지 않고, 비교·목록에서 빠진다(resolved: 미정이 답을 얻음).
INACTIVE_STATUSES = ("superseded", "reverted", "cancelled", "resolved")
OPTIONAL_ATTRIBUTES = ("maturity", "artifact_type", "summary", "github_refs")  # 항목 생성 때 속성으로 옮기는 값
GITHUB_REF_TYPES = ("issue", "pr")
GITHUB_REF_BASIS = ("explicit", "inferred")  # 명시적(본문·커밋의 #번호) / 의미상 추정
RELATED_LIMIT = 5
# 어떤 종류끼리 비교할지(종류를 넘어 비교하되 의미 있는 짝만). 피드백이 결정을 밀어내지 않게 종류로 먼저 거른다.
COMPARABLE = {
    "decision": ("decision", "requirement", "open"), "requirement": ("requirement", "decision", "open"),
    "open": ("open", "decision", "requirement", "insight"), "task": ("task", "decision"),
    "hypothesis": ("hypothesis", "insight", "decision", "open"), "insight": ("insight", "hypothesis", "open", "decision"),
    "feedback": ("feedback", "decision"),
}
SUPERSEDING_KINDS = ("decision", "requirement")
HUMAN_EDIT_PREFIX = "구조 화면에서"  # board.py가 사람이 탭에서 고친 revisions에 쓰는 사유의 앞부분
HUMAN_COORDINATION_PREFIX = "조율에서 사람이 기록"

# Ends like a question or an open suggestion: "…할까요?", "…어때요", "…인가요".
_QUESTION_RE = re.compile(r"(\?|？|할까요?|될까요?|을까요?|일까요?|어떨까요?|어때요?|인가요|나요|건가요?|는지)\s*[.!~…]*\s*$")
_ISSUE_RE = re.compile(r"#(\d+)")

Extractor = Callable[[list[dict[str, Any]]], list[dict[str, Any]]]
Judge = Callable[[dict[str, Any], list[dict[str, Any]]], dict[str, Any]]


@dataclass(frozen=True)
class AnalysisPolicy:
    """언제 분석할지.

    - Teams 대화: 60분 동안 새 메시지가 없거나, 20개·3,000토큰이 쌓이면 분석한다.
    - Notion 기록: 작성 중인 초안을 분석하지 않도록 마지막 수정 후 2시간이 지나야 분석한다(크기로는 당기지 않음).
    20개·3,000토큰은 한 번에 보내는 묶음의 상한이기도 하다.
    """

    idle_seconds: int = 60 * 60
    max_messages: int = 20
    max_tokens: int = 3000
    settle_seconds: int = 2 * 60 * 60


DEFAULT_POLICY = AnalysisPolicy()


def new_id() -> str:
    return str(uuid.uuid4())


def estimate_tokens(text: str) -> int:
    """Rough budget measure (about 4 UTF-8 bytes per token); good enough to bound a batch, not to bill."""
    return max(1, -(-len(text.encode("utf-8")) // 4))


def _parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _required(name: str, value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{name} must be a non-empty string")
    return value.strip()


# ---------------------------------------------------------------- 1. raw records


HOLD_REASONS = {"reference", "unconfirmed", "superseded"}  # unconfirmed: 이전 버전 호환용(지금은 쓰지 않음)


def record(store: Path, *, scope: str, kind: str, conversation_key: str, locator: str, text: str,
           author: str | None = None, sent_at: str | None = None, metadata: dict[str, Any] | None = None,
           received_at: str | None = None, hold: str | None = None) -> dict[str, Any]:
    """Store one raw record immediately. The same kind+locator+content is stored once.

    hold: 저장은 하되 분석하지 않을 이유 — "reference"(원문 보관용 섹션, 또는 역할이 애매해 보관만 하는 섹션).
    같은 위치의 새 버전이 들어오면, 아직 분석 전인 옛 버전은 "superseded"로 보류해 초안이 따로 분석되지 않게 한다.
    """
    if hold is not None and hold not in HOLD_REASONS - {"superseded"}:
        raise ValidationError("hold must be 'reference' or 'unconfirmed'")
    if kind not in RECORD_KINDS:
        raise ValidationError(f"kind must be one of {sorted(RECORD_KINDS)}")
    scope, conversation_key, locator = _required("scope", scope), _required("conversation_key", conversation_key), _required("locator", locator)
    text = _required("text", text)
    content_hash = db.sha256(text)
    db.initialize(store)
    ts = received_at or db.now()
    with db.write(store) as c:
        row = c.execute("SELECT id FROM sources WHERE kind = ? AND locator = ? AND content_hash = ?",
                        (kind, locator, content_hash)).fetchone()
        if row is not None:
            return {"source_id": row["id"], "created": False}
        source_id = new_id()
        meta = {"scope": scope, "conversation_key": conversation_key, "author": author, "sent_at": sent_at, **(metadata or {})}
        c.execute(
            """INSERT INTO sources(id, kind, locator, captured_at, accessible_excerpt, content, content_hash,
                                   metadata_json, parse_status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'complete')""",
            (source_id, kind, locator, sent_at or ts, text[:300], text, content_hash, to_json(meta)),
        )
        superseded = c.execute(
            """UPDATE work_records SET hold = 'superseded'
               WHERE run_id IS NULL AND source_id IN (SELECT id FROM sources WHERE kind = ? AND locator = ?)""",
            (kind, locator),
        ).rowcount
        c.execute(
            """INSERT INTO work_records(source_id, scope, conversation_key, received_at, token_estimate, hold)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (source_id, scope, conversation_key, ts, estimate_tokens(text), hold),
        )
    return {"source_id": source_id, "created": True, "superseded": superseded}


def release(store: Path, conversation_key: str, reason: str = "unconfirmed") -> int:
    """보류해 둔 기록을 분석 대기로 돌린다 (예: 섹션 역할이 확인됐을 때). 옛 버전(superseded)은 풀지 않는다."""
    if reason not in {"reference", "unconfirmed"}:
        raise ValidationError("only reference or unconfirmed records can be released")
    db.initialize(store)
    with db.write(store) as c:
        return c.execute("UPDATE work_records SET hold = NULL WHERE conversation_key = ? AND hold = ? AND run_id IS NULL",
                         (conversation_key, reason)).rowcount


# ---------------------------------------------------------------- 2. analysis policy


def due_batches(store: Path, now: str | None = None, policy: AnalysisPolicy = DEFAULT_POLICY) -> list[dict[str, Any]]:
    """Batches of pending records that the policy says to analyze now, oldest first."""
    db.initialize(store)
    current = _parse_ts(now or db.now())
    with closing(db.connect(store)) as c:
        rows = c.execute(
            """SELECT w.source_id, w.scope, w.conversation_key, w.received_at, w.token_estimate, s.kind
               FROM work_records w JOIN sources s ON s.id = w.source_id
               WHERE w.run_id IS NULL AND w.hold IS NULL ORDER BY w.conversation_key, w.received_at, w.rowid""").fetchall()
    groups: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        groups.setdefault(row["conversation_key"], []).append(row)
    batches: list[dict[str, Any]] = []
    for key, items in groups.items():
        quiet = (current - _parse_ts(items[-1]["received_at"])).total_seconds()
        if items[0]["kind"] == "notion_record":
            if quiet < policy.settle_seconds:
                continue  # 아직 작성 중일 수 있다: 크기와 상관없이 기다린다
            idle = True
        else:
            idle = quiet >= policy.idle_seconds
        chunks: list[list[sqlite3.Row]] = [[]]
        tokens = 0
        for item in items:
            chunk = chunks[-1]
            if chunk and (len(chunk) >= policy.max_messages or tokens + item["token_estimate"] > policy.max_tokens):
                chunks.append([])
                chunk, tokens = chunks[-1], 0
            chunk.append(item)
            tokens += item["token_estimate"]
        for index, chunk in enumerate(chunks):
            full = len(chunk) >= policy.max_messages or sum(r["token_estimate"] for r in chunk) >= policy.max_tokens
            last = index == len(chunks) - 1
            if not full and last and not idle:
                continue  # still filling: wait for more messages or for the conversation to go quiet
            if not full and not last:
                full = True  # an earlier chunk was closed because the next record would not fit
            batches.append({"conversation_key": key, "scope": chunk[0]["scope"],
                            "source_ids": [r["source_id"] for r in chunk],
                            "trigger": "size" if full else "idle"})
    return batches


# ---------------------------------------------------------------- 3. analysis


def is_question_like(text: str) -> bool:
    return bool(_QUESTION_RE.search(text.strip()))


def is_key_decision(areas: list[str], affected_task_count: int) -> bool:
    return bool(set(areas) & KEY_DECISION_AREAS) or affected_task_count >= 2


def normalize_item(raw: Any, source_ids: set[str]) -> tuple[dict[str, Any] | None, str]:
    """Check one extracted item. Returns (item, "") or (None, why it is not a task/decision candidate)."""
    if not isinstance(raw, dict):
        return None, "item is not an object"
    kind = raw.get("kind")
    if kind not in EXTRACT_KINDS:
        return None, f"unknown kind {kind!r}"
    if kind in {"opinion", "question"}:
        return None, f"{kind}: kept in the raw record only"
    title = str(raw.get("title") or "").strip()
    statement = str(raw.get("statement") or "").strip()
    if not title:
        return None, "no title"
    supporting = [s for s in raw.get("source_ids") or [] if isinstance(s, str) and s in source_ids]
    if not supporting:
        return None, "no supporting record in this batch"
    if is_question_like(statement or title) or is_question_like(title):
        return None, "question or open suggestion, not a decided item"
    if kind == "decision" and raw.get("certainty") != "decided":
        return None, f"decision not confirmed (certainty={raw.get('certainty')!r})"
    status = raw.get("status")
    if kind in {"task", "progress"}:
        status = status if status in TASK_STATUSES else ("open" if kind == "task" else None)
    else:
        status = "active"
    issue_refs = sorted({int(n) for n in _ISSUE_RE.findall(f"{title} {statement}")} |
                        {int(n) for n in raw.get("issue_refs") or [] if str(n).isdigit()})
    item = {
        "extracted_kind": kind,
        "kind": "task" if kind == "progress" else kind,
        "title": title,
        "statement": statement or title,
        "status": status,
        "areas": sorted({a for a in raw.get("areas") or [] if a in KEY_DECISION_AREAS}),
        "assignee": raw.get("assignee") if isinstance(raw.get("assignee"), str) else None,
        "due": raw.get("due") if isinstance(raw.get("due"), str) else None,
        "issue_refs": issue_refs,
        "source_ids": sorted(set(supporting)),
        "quote": str(raw.get("quote") or "").strip(),
    }
    return item, ""


def _item_dict(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["attributes"] = json.loads(item.pop("attributes_json"))
    return item


def find_related(c: sqlite3.Connection, scope: str, item: dict[str, Any], limit: int = RELATED_LIMIT) -> list[dict[str, Any]]:
    """Linear-triage style lookup: live items of the scope that share words or issue numbers with the item."""
    terms = [t.casefold() for t in query_terms(f"{item['title']} {item['statement']}")][:16]
    key = normalize_text(item["title"])
    refs = set(item.get("issue_refs") or [])
    scored: list[tuple[float, dict[str, Any]]] = []
    for row in c.execute(f"SELECT * FROM work_items WHERE scope = ? AND status NOT IN ({','.join('?' * len(INACTIVE_STATUSES))})",
                         (scope, *INACTIVE_STATUSES)):
        candidate = _item_dict(row)
        title, statement = candidate["title"].casefold(), candidate["statement_md"].casefold()
        score = 10.0 if candidate["normalized_title"] == key else 0.0
        score += sum(2.0 if t in title else 1.0 if t in statement else 0.0 for t in terms)
        score += 5.0 * len(refs & set(candidate["attributes"].get("issue_refs") or []))
        if score > 0:
            scored.append((score, candidate))
    scored.sort(key=lambda pair: (-pair[0], pair[1]["created_at"]))
    return [dict(candidate, score=score) for score, candidate in scored[:limit]]


def _public(related: dict[str, Any]) -> dict[str, Any]:
    """What the judge sees of an existing item."""
    return {k: related[k] for k in ("id", "kind", "title", "statement_md", "status", "attributes")}


def candidates_for(c: sqlite3.Connection, scope: str, item: dict[str, Any], extra_ids: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    """비교할 기존 항목(prior_check): 종류가 호환되는 것을 단어 겹침 순으로 RELATED_LIMIT개 + 그 밖에 점수 높은 몇 개
    + 호출부가 검색(search.query)으로 찾은 항목(extra_ids)."""
    pool = find_related(c, scope, item, limit=40)
    compatible = COMPARABLE.get(item["kind"], (item["kind"],))
    chosen = [r for r in pool if r["kind"] in compatible][:RELATED_LIMIT]
    chosen += [r for r in pool if r not in chosen][:2]
    have = {r["id"] for r in chosen}
    for item_id in extra_ids:
        if item_id in have:
            continue
        row = c.execute(f"SELECT * FROM work_items WHERE id = ? AND status NOT IN ({','.join('?' * len(INACTIVE_STATUSES))})",
                        (item_id, *INACTIVE_STATUSES)).fetchone()
        if row is not None and row["scope"] == scope and row["kind"] in compatible:
            chosen.append(dict(_item_dict(row), score=0.0))
            have.add(item_id)
    return chosen


def is_human_edited(c: sqlite3.Connection, item_id: str) -> bool:
    """사람이 탭에서 고치거나 조율에서 직접 기록한 항목은 재해석이 덮어쓰지 않는다."""
    return c.execute("""SELECT 1 FROM revisions WHERE object_type = 'work_item' AND object_id = ? AND version > 1
                        AND (reason LIKE ? OR reason LIKE ?) AND actor NOT LIKE '자동(%' LIMIT 1""",
                     (item_id, HUMAN_EDIT_PREFIX + "%", HUMAN_COORDINATION_PREFIX + "%")).fetchone() is not None


def is_important(c: sqlite3.Connection, target: dict[str, Any]) -> bool:
    """충돌했을 때 사람에게 물을 만큼 중요한 대상인가: 결정·요구사항이고, 할 일이나 다른 결정·요구사항과 이어져 있다."""
    if target["kind"] not in SUPERSEDING_KINDS:
        return False
    return c.execute("""SELECT 1 FROM work_links l JOIN work_items o
                        ON o.id = CASE WHEN l.source_id = ? THEN l.target_id ELSE l.source_id END
                        WHERE (l.source_id = ? OR l.target_id = ?) AND l.type IN ('AFFECTS', 'SUPERSEDES')
                        AND o.kind IN ('task', 'decision', 'requirement') LIMIT 1""",
                     (target["id"], target["id"], target["id"])).fetchone() is not None


def _target_allowed(change_type: str, item_kind: str, target_kind: str) -> bool:
    if change_type in ("unchanged", "supplementary", "conflicting"):
        return target_kind == item_kind
    if change_type == "superseding":
        return item_kind in SUPERSEDING_KINDS and target_kind == item_kind
    if change_type == "alternative":
        return item_kind in ("decision", "requirement", "hypothesis") and target_kind in SUPERSEDING_KINDS
    if change_type == "resolving":
        return target_kind == "open" and item_kind != "open"
    return False


def _judge(item: dict[str, Any], related: list[dict[str, Any]], judge: Judge, strict: bool = False) -> dict[str, Any] | None:
    """Decide the change type. Rules first; the judge only for real comparisons; its answer is validated.
    strict=False(예전 분석 경로): 판정에 실패하면 사람이 비교하도록 conflicting으로 돌려준다.
    strict=True(v2.1 해석 경로): 실패는 예외로 올린다. 호출부가 오류로 기록하고 다시 시도한다(의사결정 충돌이 아니다)."""
    same_kind = [r for r in related if r["kind"] == item["kind"]]
    tasks = {r["id"] for r in related if r["kind"] == "task"}
    for r in same_kind:
        if r["normalized_title"] == normalize_text(item["title"]) and normalize_text(r["statement_md"]) == normalize_text(item["statement"]):
            return {"change_type": "unchanged", "target": r, "affected": [], "judged_by": "rule",
                    "rationale": "제목과 내용이 기존 기록과 같다.", "decidable": True}
    if not related:
        if item["extracted_kind"] == "progress":
            return None
        return {"change_type": "proposed", "target": None, "affected": [], "judged_by": "rule",
                "rationale": "관련된 기존 항목이 없다.", "decidable": True}
    try:
        answer = judge(item, [_public(r) for r in related])
        if not isinstance(answer, dict):
            raise ContractError("judge must return an object")
        change_type = answer.get("change_type")
        if change_type not in CHANGE_TYPES:
            raise ContractError(f"unknown change_type {change_type!r}")
        target_id = answer.get("target_id")
        target = next((r for r in related if r["id"] == target_id), None)
        if change_type == "proposed":
            target = None
        elif target is None:
            raise ContractError(f"{change_type} needs a target_id among the compared items")
        elif not _target_allowed(change_type, item["kind"], target["kind"]):
            raise ContractError(f"{change_type} cannot link a {item['kind']} to a {target['kind']}")
        affected = [t for t in answer.get("affected_task_ids") or [] if t in tasks]
        decidable = answer.get("decidable")
        decidable = bool(decidable) if isinstance(decidable, bool) else change_type != "conflicting"
        maturity = item.get("maturity")
        rationale = str(answer.get("rationale") or "").strip() or "(판정 근거 없음)"
        if change_type == "superseding" and maturity not in (None, "agreed"):
            # 합의 근거가 없는 새 내용은 기존 결정을 바꾸지 못한다: 대안으로 남긴다
            change_type, rationale = "alternative", rationale + f" (새 항목의 성숙도가 {maturity}라 대체하지 않고 대안으로 둔다.)"
        result = {"change_type": change_type, "target": target, "affected": sorted(set(affected)), "judged_by": "judge",
                  "rationale": rationale, "decidable": decidable}
    except Exception as error:  # a bad or failed judgment must still reach a human, never pass silently
        if strict:
            raise
        top = same_kind[0] if same_kind else None
        result = {"change_type": "conflicting" if top else "proposed", "target": top, "affected": [], "judged_by": "fallback",
                  "rationale": f"자동 판정을 쓸 수 없어 사람이 비교해야 한다: {error}", "decidable": False}
    if item["extracted_kind"] == "progress" and result["change_type"] in {"proposed", "superseding"}:
        return None  # a progress report never creates or replaces anything on its own
    if result["change_type"] == "proposed":
        # A new item whose title is already live would only collide on approval: compare with that one instead.
        twin = next((r for r in same_kind if r["normalized_title"] == normalize_text(item["title"])), None)
        if twin is not None:
            result.update(change_type="supplementary", target=twin,
                          rationale=result["rationale"] + " (같은 제목의 기존 항목이 있어 보충 후보로 바꿨다.)")
    return result


def _fingerprint(scope: str, item: dict[str, Any], judgment: dict[str, Any]) -> str:
    target = judgment["target"]["id"] if judgment["target"] else None
    return db.sha256([scope, item["kind"], judgment["change_type"], target, normalize_text(item["title"]), item["source_ids"]])


def analyze_batch(store: Path, source_ids: list[str], extract: Extractor, judge: Judge) -> dict[str, Any]:
    """Turn one batch of raw records into change candidates. The same batch is analyzed once."""
    ids = sorted(set(source_ids))
    if not ids:
        raise ValidationError("source_ids must not be empty")
    batch_hash = db.sha256(ids)
    db.initialize(store)
    run = runs.receive(store, f"work-analysis:{batch_hash}")
    if run["status"] == "succeeded":
        return dict(json.loads(run["result_json"]), reused=True)
    run_id = run["id"]
    try:
        marks = ",".join("?" * len(ids))
        with closing(db.connect(store)) as c:
            rows = c.execute(
                f"""SELECT s.id, s.kind, s.locator, s.content, s.metadata_json, w.scope, w.conversation_key
                    FROM sources s JOIN work_records w ON w.source_id = s.id WHERE s.id IN ({marks})
                    ORDER BY w.received_at, w.rowid""", tuple(ids)).fetchall()
        if len(rows) != len(ids):
            raise NotFoundError("some source_ids are not stored work records")
        scopes = {r["scope"] for r in rows}
        if len(scopes) != 1:
            raise ValidationError("one batch must belong to one scope")
        scope = scopes.pop()
        records = [{"source_id": r["id"], "kind": r["kind"], "locator": r["locator"], "text": r["content"],
                    "author": json.loads(r["metadata_json"] or "{}").get("author"),
                    "sent_at": json.loads(r["metadata_json"] or "{}").get("sent_at")} for r in rows]
        content = {r["id"]: r["content"] for r in rows}
        runs.advance(store, run_id, "normalized", input_hash=batch_hash)

        extracted = extract(records)
        if not isinstance(extracted, list):
            raise ValidationError("extract must return a list of items")
        plans: list[tuple[dict[str, Any], dict[str, Any], list[str]]] = []
        ignored: list[dict[str, Any]] = []
        with closing(db.connect(store)) as c:
            for index, raw in enumerate(extracted):
                item, why = normalize_item(raw, set(ids))
                if item is None:
                    ignored.append({"index": index, "reason": why, "title": raw.get("title") if isinstance(raw, dict) else None,
                                    "source_ids": raw.get("source_ids") if isinstance(raw, dict) else None})
                    continue
                related = find_related(c, scope, item)
                judgment = _judge(item, related, judge)
                if judgment is None:
                    ignored.append({"index": index, "reason": "progress report without a matching task",
                                    "title": item["title"], "source_ids": item["source_ids"]})
                    continue
                if (item["kind"] == "decision" and judgment["change_type"] == "proposed"
                        and not is_key_decision(item["areas"], len(judgment["affected"]))):
                    ignored.append({"index": index, "reason": "not a key decision (no goal/scope/architecture/budget/schedule, "
                                    "affects fewer than 2 tasks)", "title": item["title"], "source_ids": item["source_ids"]})
                    continue
                plans.append((item, judgment, [r["id"] for r in related]))
        runs.advance(store, run_id, "planned", summary={"candidates": len(plans), "ignored": len(ignored)})

        with db.write(store) as c:
            current = c.execute("SELECT status, result_json FROM runs WHERE id = ?", (run_id,)).fetchone()
            if current["status"] == "succeeded":  # a concurrent attempt committed first
                return dict(json.loads(current["result_json"]), reused=True)
            ts = db.now()
            created: list[dict[str, Any]] = []
            existing: list[str] = []
            for item, judgment, compared in plans:
                candidate_id, is_new = _insert_candidate(c, scope, run_id, item, judgment, compared, content, ts)
                (created.append({"id": candidate_id, "change_type": judgment["change_type"], "item_kind": item["kind"],
                                 "target_id": judgment["target"]["id"] if judgment["target"] else None,
                                 "title": item["title"]}) if is_new else existing.append(candidate_id))
            c.execute(f"UPDATE work_records SET run_id = ? WHERE source_id IN ({marks}) AND run_id IS NULL", (run_id, *ids))
            result = {"run_id": run_id, "scope": scope, "source_ids": ids, "candidates": created,
                      "existing_candidates": existing, "ignored": ignored, "reused": False}
            c.execute(
                """UPDATE runs SET stage = 'committed', status = 'succeeded', input_hash = ?, result_json = ?, error = NULL,
                                   error_kind = NULL, updated_at = ? WHERE id = ?""",
                (batch_hash, to_json(result), ts, run_id),
            )
            return result
    except BaseException as error:
        runs.fail(store, run_id, error)
        raise


def _insert_candidate(c: sqlite3.Connection, scope: str, run_id: str, item: dict[str, Any], judgment: dict[str, Any],
                      compared: list[str], content: dict[str, str], ts: str) -> tuple[str, bool]:
    fingerprint = _fingerprint(scope, item, judgment)
    row = c.execute("SELECT id FROM change_candidates WHERE fingerprint = ?", (fingerprint,)).fetchone()
    if row is not None:
        return row["id"], False
    target = judgment["target"]
    candidate_id = new_id()
    proposal = {k: item[k] for k in ("title", "statement", "status", "areas", "assignee", "due", "issue_refs", "extracted_kind")}
    proposal.update({k: item[k] for k in OPTIONAL_ATTRIBUTES if item.get(k)})  # 성숙도 등(없으면 넣지 않아 기존 지문·형식 그대로)
    proposal["affected_task_ids"] = judgment["affected"]
    status = "recorded" if judgment["change_type"] == "unchanged" else "pending"
    c.execute(
        """INSERT INTO change_candidates(id, fingerprint, scope, run_id, change_type, item_kind, target_id, target_version,
                                         compared_ids_json, proposal_json, rationale, judged_by, status, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (candidate_id, fingerprint, scope, run_id, judgment["change_type"], item["kind"], target["id"] if target else None,
         target["version"] if target else None, to_json(compared), to_json(proposal), judgment["rationale"],
         judgment["judged_by"], status, ts, ts),
    )
    for source_id in item["source_ids"]:
        text = content[source_id]
        quote = item["quote"]
        excerpt = quote if quote and quote in text else text[:300]
        evidence_id = new_id()
        c.execute(
            """INSERT INTO evidence(id, source_id, section_locator, excerpt, evidence_kind) VALUES (?, ?, ?, ?, 'observed')""",
            (evidence_id, source_id, "quote" if excerpt == quote else "record", excerpt),
        )
        c.execute("INSERT INTO object_evidence(object_type, object_id, evidence_id) VALUES ('change_candidate', ?, ?)",
                  (candidate_id, evidence_id))
    return candidate_id, True


def analyze_due(store: Path, extract: Extractor, judge: Judge, now: str | None = None,
                policy: AnalysisPolicy = DEFAULT_POLICY) -> list[dict[str, Any]]:
    """Analyze every batch the policy says is due. A failing batch is kept on its run and retried next time."""
    results = []
    for batch in due_batches(store, now, policy):
        try:
            results.append(analyze_batch(store, batch["source_ids"], extract, judge))
        except Exception as error:
            results.append({"source_ids": batch["source_ids"], "error": str(error)})
    return results


# ---------------------------------------------------------------- 4. review (DACI: the approver is accountable)


def _candidate(c: sqlite3.Connection, candidate_id: str) -> sqlite3.Row:
    row = c.execute("SELECT * FROM change_candidates WHERE id = ?", (candidate_id,)).fetchone()
    if row is None:
        raise NotFoundError(f"change candidate not found: {candidate_id}")
    return row


def _evidence_ids(c: sqlite3.Connection, object_type: str, object_id: str) -> list[str]:
    return [r["evidence_id"] for r in c.execute(
        "SELECT evidence_id FROM object_evidence WHERE object_type = ? AND object_id = ?", (object_type, object_id))]


def _snapshot(c: sqlite3.Connection, item_id: str) -> dict[str, Any]:
    return _item_dict(c.execute("SELECT * FROM work_items WHERE id = ?", (item_id,)).fetchone())


def _write_item(c: sqlite3.Connection, item_id: str, changes: dict[str, Any], reason: str, actor: str, ts: str) -> int:
    row = c.execute("SELECT version FROM work_items WHERE id = ?", (item_id,)).fetchone()
    version = row["version"] + 1
    sets = ", ".join(f"{k} = ?" for k in changes)
    values = [to_json(v) if k == "attributes_json" else v for k, v in changes.items()]
    c.execute(f"UPDATE work_items SET {sets}, version = ?, updated_at = ? WHERE id = ?", (*values, version, ts, item_id))
    db.add_revision(c, "work_item", item_id, version, _snapshot(c, item_id), reason, ts, new_id(), actor=actor)
    _index(c, [item_id])
    return version


def _index(c: sqlite3.Connection, item_ids: list[str]) -> None:
    """검색 색인을 같은 트랜잭션에서 갱신한다(search는 core를 쓰므로 늦게 불러온다)."""
    from . import search

    search.index_items(c, item_ids)


def github_refs(attributes: dict[str, Any]) -> list[dict[str, Any]]:
    """항목의 GitHub 연결. `github_refs`(명시/추정 구분)가 있으면 그것을, 없으면 예전 `issue_refs`를 명시적 이슈로 읽는다."""
    refs = attributes.get("github_refs")
    if isinstance(refs, list):
        return [r for r in refs if isinstance(r, dict) and r.get("type") in GITHUB_REF_TYPES and str(r.get("number")).isdigit()]
    return [{"type": "issue", "number": int(n), "basis": "explicit"} for n in attributes.get("issue_refs") or [] if str(n).isdigit()]


def issue_numbers(attributes: dict[str, Any], explicit_only: bool = True) -> list[int]:
    """연결된 GitHub 이슈 번호. 기본은 명시적 연결만(담당 배정처럼 실제로 바꾸는 일에는 추정 연결을 쓰지 않는다)."""
    return sorted({int(r["number"]) for r in github_refs(attributes)
                   if r["type"] == "issue" and (not explicit_only or r.get("basis", "explicit") == "explicit")})


def _clean_optional_attributes(proposal: dict[str, Any]) -> dict[str, Any]:
    """생성 때 받는 선택 속성(성숙도·산출물 종류·요약·GitHub 연결)을 검증해서 돌려준다."""
    out: dict[str, Any] = {}
    for key in OPTIONAL_ATTRIBUTES:
        value = proposal.get(key)
        if not value:
            continue
        if key == "maturity" and value not in MATURITY:
            raise ValidationError(f"maturity must be one of {list(MATURITY)}")
        if key == "github_refs":
            if not isinstance(value, list):
                raise ValidationError("github_refs must be a list")
            for ref in value:
                if (not isinstance(ref, dict) or ref.get("type") not in GITHUB_REF_TYPES or not str(ref.get("number")).isdigit()
                        or ref.get("basis") not in GITHUB_REF_BASIS):
                    raise ValidationError(f"github_refs entries need type {list(GITHUB_REF_TYPES)}, number, basis {list(GITHUB_REF_BASIS)}")
            value = [{"type": r["type"], "number": int(r["number"]), "basis": r["basis"]} for r in value]
        out[key] = value
    return out


def _create_item(c: sqlite3.Connection, scope: str, kind: str, proposal: dict[str, Any], reason: str, actor: str, ts: str) -> str:
    title = proposal["title"]
    if kind not in KNOWLEDGE_KINDS:
        raise ValidationError(f"kind must be one of {list(KNOWLEDGE_KINDS)}")
    optional = _clean_optional_attributes(proposal)
    clash = c.execute(f"SELECT id FROM work_items WHERE scope = ? AND kind = ? AND normalized_title = ? "
                      f"AND status NOT IN ({','.join('?' * len(INACTIVE_STATUSES))})",
                      (scope, kind, normalize_text(title), *INACTIVE_STATUSES)).fetchone()
    if clash is not None:
        raise ConflictError(f"DUPLICATE: a live {kind} with this title already exists ({clash['id']}); re-analyze to compare with it")
    item_id = new_id()
    attributes = {k: proposal.get(k) for k in ("assignee", "due") if proposal.get(k)}
    attributes["issue_refs"] = proposal.get("issue_refs") or []
    attributes.update(optional)
    if kind == "decision":
        attributes["areas"] = proposal.get("areas") or []
        attributes["accountable"] = actor
    status = proposal.get("status") if kind == "task" and proposal.get("status") in TASK_STATUSES else ("open" if kind == "task" else "active")
    c.execute(
        """INSERT INTO work_items(id, scope, kind, title, normalized_title, statement_md, status, attributes_json, version,
                                  created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
        (item_id, scope, kind, title, normalize_text(title), proposal["statement"], status, to_json(attributes), ts, ts),
    )
    db.add_revision(c, "work_item", item_id, 1, _snapshot(c, item_id), reason, ts, new_id(), actor=actor)
    _index(c, [item_id])
    return item_id


def _link(c: sqlite3.Connection, source_id: str, target_id: str, link_type: str, candidate_id: str, ts: str) -> None:
    c.execute("""INSERT OR IGNORE INTO work_links(id, source_id, target_id, type, candidate_id, created_at)
                 VALUES (?, ?, ?, ?, ?, ?)""", (new_id(), source_id, target_id, link_type, candidate_id, ts))


def _tasks_of(c: sqlite3.Connection, decision_id: str) -> list[dict[str, Any]]:
    return [_item_dict(r) for r in c.execute(
        """SELECT t.* FROM work_links l JOIN work_items t ON t.id = l.target_id
           WHERE l.source_id = ? AND l.type = 'AFFECTS' AND t.kind = 'task' ORDER BY t.created_at""", (decision_id,))]


def approve(store: Path, candidate_id: str, actor: str, note: str = "") -> dict[str, Any]:
    """Apply one pending candidate. Fails if the compared item changed since it was judged."""
    actor = _required("actor", actor)
    db.initialize(store)
    ts = db.now()
    with db.write(store) as c:
        cand = _candidate(c, candidate_id)
        if cand["status"] != "pending":
            raise ConflictError(f"candidate {candidate_id} is {cand['status']}, not pending")
        proposal = json.loads(cand["proposal_json"])
        scope, kind, change_type = cand["scope"], cand["item_kind"], cand["change_type"]
        target = c.execute("SELECT * FROM work_items WHERE id = ?", (cand["target_id"],)).fetchone() if cand["target_id"] else None
        if cand["target_id"] and (target is None or target["version"] != cand["target_version"]):
            current = target["version"] if target is not None else "missing"
            raise ConflictError(f"STALE_CANDIDATE: {cand['target_id']} changed since this candidate was judged "
                                f"(v{cand['target_version']} → v{current}); re-analyze the records")
        reason = f"candidate {candidate_id} ({change_type}) approved by {actor}" + (f": {note}" if note else "")
        evidence = _evidence_ids(c, "change_candidate", candidate_id)
        live_tasks = {r["id"] for r in c.execute("SELECT id FROM work_items WHERE scope = ? AND kind = 'task'", (scope,))}
        affected = [t for t in proposal.get("affected_task_ids") or [] if t in live_tasks]
        impacted: list[dict[str, Any]] = []

        if change_type == "proposed":
            item_id = _create_item(c, scope, kind, proposal, reason, actor, ts)
            for task_id in affected:
                _link(c, item_id, task_id, LINK_AFFECTS, candidate_id, ts)
        elif change_type == "supplementary" or (change_type == "conflicting" and kind == "task"):
            item_id = target["id"]
            attributes = json.loads(target["attributes_json"])
            changes: dict[str, Any] = {}
            if change_type == "supplementary":
                if normalize_text(proposal["statement"]) not in normalize_text(target["statement_md"]):
                    changes["statement_md"] = f"{target['statement_md']}\n\n보충 ({ts[:10]}): {proposal['statement']}"
                for key in ("assignee", "due"):
                    if proposal.get(key) and not attributes.get(key):
                        attributes[key] = proposal[key]
            else:  # an approved conflict on a task replaces what the task says
                changes["statement_md"] = proposal["statement"]
                for key in ("assignee", "due"):
                    if proposal.get(key):
                        attributes[key] = proposal[key]
            attributes["issue_refs"] = sorted(set(attributes.get("issue_refs") or []) | set(proposal.get("issue_refs") or []))
            if kind == "decision":
                attributes["areas"] = sorted(set(attributes.get("areas") or []) | set(proposal.get("areas") or []))
            if kind == "task" and proposal.get("status") in TASK_STATUSES and proposal["status"] != target["status"]:
                changes["status"] = proposal["status"]
            changes["attributes_json"] = attributes
            _write_item(c, item_id, changes, reason, actor, ts)
            for task_id in affected:
                if kind == "decision":
                    _link(c, item_id, task_id, LINK_AFFECTS, candidate_id, ts)
            if kind == "decision":
                impacted = _tasks_of(c, item_id)
        elif change_type == "alternative":  # 기존 결정은 그대로 두고, 다른 가안·조언을 대안으로 남긴다
            item_id = _create_item(c, scope, kind, proposal, reason, actor, ts)
            _link(c, item_id, target["id"], LINK_ALTERNATIVE_TO, candidate_id, ts)
            for task_id in affected:
                _link(c, item_id, task_id, LINK_AFFECTS, candidate_id, ts)
        elif change_type == "resolving":  # 미정에 답하는 항목: 미정은 resolved, 새 항목이 RESOLVES로 잇는다
            item_id = _create_item(c, scope, kind, proposal, reason, actor, ts)
            _write_item(c, target["id"], {"status": "resolved"}, reason, actor, ts)
            _link(c, item_id, target["id"], LINK_RESOLVES, candidate_id, ts)
            for task_id in affected:
                _link(c, item_id, task_id, LINK_AFFECTS, candidate_id, ts)
        else:  # superseding, or an approved conflict on a decision: decisions are replaced, never rewritten (ADR)
            old_id = target["id"]
            impacted_ids = {t["id"] for t in _tasks_of(c, old_id)} | set(affected)
            _write_item(c, old_id, {"status": "superseded"}, reason, actor, ts)
            item_id = _create_item(c, scope, kind if kind in SUPERSEDING_KINDS else "decision", proposal, reason, actor, ts)
            _link(c, item_id, old_id, LINK_SUPERSEDES, candidate_id, ts)
            for task_id in sorted(impacted_ids):
                _link(c, item_id, task_id, LINK_AFFECTS, candidate_id, ts)
            impacted = _tasks_of(c, item_id)

        for evidence_id in evidence:
            c.execute("INSERT OR IGNORE INTO object_evidence(object_type, object_id, evidence_id) VALUES ('work_item', ?, ?)",
                      (item_id, evidence_id))
        c.execute("""UPDATE change_candidates SET status = 'approved', decided_by = ?, decided_at = ?, decision_note = ?,
                                                  applied_item_id = ?, updated_at = ? WHERE id = ?""",
                  (actor, ts, note or None, item_id, ts, candidate_id))
        return {"candidate_id": candidate_id, "change_type": change_type, "item": _snapshot(c, item_id),
                "impacted_tasks": [{"id": t["id"], "title": t["title"], "status": t["status"]} for t in impacted]}


def reject(store: Path, candidate_id: str, actor: str, note: str) -> dict[str, Any]:
    """Close a candidate without touching any Task or Decision."""
    actor, note = _required("actor", actor), _required("note", note)
    db.initialize(store)
    ts = db.now()
    with db.write(store) as c:
        cand = _candidate(c, candidate_id)
        if cand["status"] != "pending":
            raise ConflictError(f"candidate {candidate_id} is {cand['status']}, not pending")
        c.execute("""UPDATE change_candidates SET status = 'rejected', decided_by = ?, decided_at = ?, decision_note = ?,
                                                  updated_at = ? WHERE id = ?""", (actor, ts, note, ts, candidate_id))
    return {"candidate_id": candidate_id, "status": "rejected"}


# ---------------------------------------------------------------- 5. reading


def _candidate_record(c: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["proposal"] = json.loads(item.pop("proposal_json"))
    item["compared_ids"] = json.loads(item.pop("compared_ids_json"))
    item["evidence"] = [dict(r) for r in c.execute(
        """SELECT e.id AS evidence_id, e.excerpt, e.section_locator, s.id AS source_id, s.kind AS source_kind, s.locator,
                  s.captured_at, s.metadata_json FROM object_evidence oe JOIN evidence e ON e.id = oe.evidence_id
           JOIN sources s ON s.id = e.source_id WHERE oe.object_type = 'change_candidate' AND oe.object_id = ?
           ORDER BY s.captured_at""", (row["id"],))]
    for ev in item["evidence"]:
        ev["author"] = json.loads(ev.pop("metadata_json") or "{}").get("author")
    return item


def list_candidates(store: Path, scope: str | None = None, status: str | None = "pending") -> dict[str, Any]:
    if status is not None and status not in CANDIDATE_STATUSES:
        raise ValidationError(f"status must be one of {sorted(CANDIDATE_STATUSES)}")
    db.initialize(store)
    with closing(db.connect(store)) as c:
        sql, args = "SELECT * FROM change_candidates WHERE 1 = 1", []
        if scope:
            sql, args = sql + " AND scope = ?", [*args, scope]
        if status:
            sql, args = sql + " AND status = ?", [*args, status]
        items = []
        for row in c.execute(sql + " ORDER BY created_at, rowid", args).fetchall():
            item = _candidate_record(c, row)
            if row["target_id"] and row["item_kind"] == "decision":
                item["impacted_tasks"] = [{"id": t["id"], "title": t["title"]} for t in _tasks_of(c, row["target_id"])]
            items.append(item)
    return {"candidates": items}


def impacted_tasks(store: Path, decision_id: str) -> dict[str, Any]:
    """Tasks that a decision affects, and the open change candidates that would change the decision."""
    db.initialize(store)
    with closing(db.connect(store)) as c:
        row = c.execute("SELECT * FROM work_items WHERE id = ?", (decision_id,)).fetchone()
        if row is None or row["kind"] != "decision":
            raise NotFoundError(f"decision not found: {decision_id}")
        pending = [{"id": r["id"], "change_type": r["change_type"]} for r in c.execute(
            "SELECT id, change_type FROM change_candidates WHERE target_id = ? AND status = 'pending'", (decision_id,))]
        superseded_by = [r["source_id"] for r in c.execute(
            "SELECT source_id FROM work_links WHERE target_id = ? AND type = 'SUPERSEDES'", (decision_id,))]
        return {"decision": _item_dict(row), "tasks": _tasks_of(c, decision_id), "pending_changes": pending,
                "superseded_by": superseded_by}


def trace(store: Path, object_id: str) -> dict[str, Any]:
    """History of a Task/Decision (or one candidate) down to the raw records it came from."""
    db.initialize(store)
    with closing(db.connect(store)) as c:
        cand = c.execute("SELECT * FROM change_candidates WHERE id = ?", (object_id,)).fetchone()
        if cand is not None:
            return {"object_type": "change_candidate", "candidate": _candidate_record(c, cand)}
        row = c.execute("SELECT * FROM work_items WHERE id = ?", (object_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"not found: {object_id}")
        revisions = [dict(r, snapshot=json.loads(r["snapshot_json"])) for r in c.execute(
            """SELECT version, reason, actor, changed_at, snapshot_json FROM revisions
               WHERE object_type = 'work_item' AND object_id = ? ORDER BY version""", (object_id,))]
        for r in revisions:
            r.pop("snapshot_json")
        candidates = [_candidate_record(c, r) for r in c.execute(
            "SELECT * FROM change_candidates WHERE target_id = ? OR applied_item_id = ? ORDER BY created_at, rowid",
            (object_id, object_id))]
        links = [dict(r) for r in c.execute(
            "SELECT id, source_id, target_id, type, candidate_id, created_at FROM work_links WHERE source_id = ? OR target_id = ?",
            (object_id, object_id))]
        return {"object_type": "work_item", "item": _item_dict(row), "revisions": revisions, "candidates": candidates,
                "links": links}
