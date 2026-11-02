"""분석 실행 기록: received → normalized → planned → committed. build-log(buildlog/runs.py)에서 가져왔다.

단계가 바뀔 때마다 작은 트랜잭션으로 커밋해서, 실패하면 어디까지 갔는지와 오류가 남는다.
같은 키로 다시 시도하면 실패한 행을 재사용하고, 이미 성공한 행은 건드리지 않는다.
"""
from __future__ import annotations

import sqlite3
import uuid
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
