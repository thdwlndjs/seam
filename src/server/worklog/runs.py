"""분석 실행 기록: received → normalized → planned → committed. build-log(buildlog/runs.py)에서 가져왔다.

단계가 바뀔 때마다 작은 트랜잭션으로 커밋해서, 실패하면 어디까지 갔는지와 오류가 남는다.
같은 키로 다시 시도하면 실패한 행을 재사용하고, 이미 성공한 행은 건드리지 않는다.
"""
from __future__ import annotations

import sqlite3
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any

from . import db
from .db import ConflictError, NotFoundError, ValidationError, to_json


def error_kind(error: BaseException) -> str:
    if isinstance(error, ConflictError):
        return "conflict"
    if isinstance(error, NotFoundError):
        return "not_found"
    if isinstance(error, ValidationError):
        return "validation"
    if isinstance(error, sqlite3.Error):
        return "store"
    return "internal"


def receive(store: Path, key: str) -> dict[str, Any]:
    ts = db.now()
    with db.write(store) as c:
        row = c.execute("SELECT * FROM runs WHERE idempotency_key = ?", (key,)).fetchone()
        if row is None:
            run_id = str(uuid.uuid4())
            c.execute(
                """INSERT INTO runs(id, idempotency_key, input_hash, stage, status, attempts, created_at, updated_at)
                   VALUES (?, ?, '', 'received', 'running', 1, ?, ?)""",
                (run_id, key, ts, ts),
            )
            return dict(c.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone())
        if row["status"] == "succeeded":
            return dict(row)
        c.execute(
            """UPDATE runs SET stage = 'received', status = 'running', attempts = attempts + 1, error = NULL,
                               error_kind = NULL, updated_at = ? WHERE id = ? AND status != 'succeeded'""",
            (ts, row["id"]),
        )
        return dict(c.execute("SELECT * FROM runs WHERE id = ?", (row["id"],)).fetchone())


def advance(store: Path, run_id: str, stage: str, input_hash: str | None = None, summary: dict[str, Any] | None = None) -> None:
    with db.write(store) as c:
        c.execute(
            """UPDATE runs SET stage = ?, input_hash = COALESCE(?, input_hash),
                               plan_summary_json = COALESCE(?, plan_summary_json), updated_at = ?
               WHERE id = ? AND status = 'running'""",
            (stage, input_hash, to_json(summary) if summary is not None else None, db.now(), run_id),
        )


def fail(store: Path, run_id: str, error: BaseException) -> None:
    try:
        with db.write(store) as c:
            c.execute(
                """UPDATE runs SET status = 'failed', error_kind = ?, error = ?, updated_at = ?
                   WHERE id = ? AND status != 'succeeded'""",
                (error_kind(error), str(error)[:2000], db.now(), run_id),
            )
    except sqlite3.Error:
        pass  # 원래 오류가 더 중요하다


# ── 기술 오류 기록(v2.1 Phase 2) ──────────────────────────────
# 인용 불일치·계약 위반·판정 실패는 지식이 아니라 처리 기록이다. 항목을 만들지 않고, 사람에게 확인을 요청하지도 않는다
# (needs_discussion은 판단할 수 없는 중요한 의사결정 충돌만). 다음 주기에 다시 처리하고, 3번 실패하면 멈춘다.
MAX_ATTEMPTS = 3
STAGES = ("interpret", "reconcile")  # 오류를 남기는 처리 단계(explore는 선택 결과 저장에 쓴다)


def key_for(stage: str, scope: str, ref: str) -> str:
    return f"{stage}:{scope}:{ref}"


def get(store: Path, key: str) -> dict[str, Any] | None:
    db.initialize(store)
    with closing(db.connect(store)) as c:
        row = c.execute("SELECT * FROM runs WHERE idempotency_key = ?", (key,)).fetchone()
    return dict(row) if row else None


def put(store: Path, key: str, stage: str, status: str, result: Any = None, error_kind: str | None = None,
        error: str | None = None, count: bool = False, summary: Any = None) -> dict[str, Any]:
    """한 줄을 만들거나 고친다. count=True면 시도 횟수를 하나 올린다(새로 만들 때는 1)."""
    ts = db.now()
    with db.write(store) as c:
        row = c.execute("SELECT * FROM runs WHERE idempotency_key = ?", (key,)).fetchone()
        if row is None:
            run_id = str(uuid.uuid4())
            c.execute(
                """INSERT INTO runs(id, idempotency_key, input_hash, stage, status, attempts, error_kind, error,
                                    plan_summary_json, result_json, created_at, updated_at)
                   VALUES (?, ?, '', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (run_id, key, stage, status, 1 if count else 0, error_kind, (error or "")[:2000] or None,
                 to_json(summary) if summary is not None else None, to_json(result) if result is not None else None, ts, ts))
        else:
            c.execute(
                """UPDATE runs SET stage = ?, status = ?, attempts = attempts + ?, error_kind = ?, error = ?,
                                   plan_summary_json = COALESCE(?, plan_summary_json), result_json = COALESCE(?, result_json),
                                   updated_at = ? WHERE id = ?""",
                (stage, status, 1 if count else 0, error_kind, (error or "")[:2000] or None,
                 to_json(summary) if summary is not None else None, to_json(result) if result is not None else None, ts, row["id"]))
        return dict(c.execute("SELECT * FROM runs WHERE idempotency_key = ?", (key,)).fetchone())


def report(store: Path, key: str, stage: str, kind: str, error: str, result: Any = None, count: bool = True) -> dict[str, Any]:
    """기술 오류 하나를 남긴다. 한도(429)·대기열 Busy처럼 내용 때문이 아닌 실패는 count=False로 횟수에 넣지 않는다."""
    return put(store, key, stage, "failed", result=result, error_kind=kind, error=error, count=count)


def succeed(store: Path, key: str, result: Any = None) -> None:
    """이 키의 오류 기록이 있으면 성공으로 닫는다(없으면 아무것도 하지 않는다)."""
    row = get(store, key)
    if row is not None and row["status"] == "failed":
        put(store, key, row["stage"], "succeeded", result=result)


def exhausted(store: Path, key: str) -> bool:
    row = get(store, key)
    return bool(row and row["status"] == "failed" and row["attempts"] >= MAX_ATTEMPTS)


def retryable(store: Path, scope: str, stage: str) -> list[dict[str, Any]]:
    """다시 시도할 오류 기록(실패 상태이고 횟수가 한도 안)."""
    db.initialize(store)
    with closing(db.connect(store)) as c:
        rows = c.execute("""SELECT * FROM runs WHERE stage = ? AND status = 'failed' AND attempts < ?
                            AND idempotency_key LIKE ? ORDER BY updated_at""", (stage, MAX_ATTEMPTS, f"{stage}:{scope}:%")).fetchall()
    return [dict(r) for r in rows]


def stuck(store: Path, scope: str) -> list[dict[str, Any]]:
    """3번 실패해서 멈춘 오류 기록(관리자 요약 줄에 건수를 보이고, `reprocess`로 다시 시도하게 한다)."""
    db.initialize(store)
    out: list[dict[str, Any]] = []
    with closing(db.connect(store)) as c:
        for stage in STAGES:
            out += [dict(r) for r in c.execute(
                """SELECT * FROM runs WHERE stage = ? AND status = 'failed' AND attempts >= ? AND idempotency_key LIKE ?""",
                (stage, MAX_ATTEMPTS, f"{stage}:{scope}:%"))]
    return out


def reprocess(store: Path, scope: str, error_kind: str | None = None) -> int:
    """멈춘(또는 실패한) 오류 기록의 횟수를 0으로 돌려 다음 주기에 다시 처리하게 한다. 돌려준 수 = 다시 열린 기록."""
    db.initialize(store)
    count = 0
    with db.write(store) as c:
        for stage in STAGES:
            rows = c.execute("""SELECT id, error_kind FROM runs WHERE stage = ? AND status = 'failed' AND idempotency_key LIKE ?""",
                             (stage, f"{stage}:{scope}:%")).fetchall()
            for row in rows:
                if error_kind and row["error_kind"] != error_kind:
                    continue
                c.execute("UPDATE runs SET attempts = 0, updated_at = ? WHERE id = ?", (db.now(), row["id"]))
                count += 1
    return count
