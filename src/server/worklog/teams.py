"""팀 등록부와 명단(번호→팀), 설정 변경 기록. 문서: docs/seam/01-setup.md, 시나리오 A(처음 온 팀원).

- 어느 팀인지는 Teams 표시 이름 `User <번호>`의 번호로 명단에서 찾는다. 번호를 꺼내는 곳은 `member_no`(이 파일) 한 군데다.
- 명단이 비어 있으면(명단을 쓰지 않는 설치·테스트) 예전 방식(1:1 채팅 = 사람 단위 범위)을 그대로 쓴다.
- 팀 연결 상태: new → setting_up(한 사람이 진행 중) → connected. 같은 팀에서 둘이 동시에 연결하면 먼저 끝낸 쪽만 저장된다.
  setting_up으로 STALE_MINUTES 넘게 멈춘 팀은 다른 사람이 다시 시작할 수 있다.
- 번호·토큰은 로그·예외 메시지에 넣지 않는다. 설정 변경 기록에는 값 없이 누가·언제·무엇을 바꿨는지만 남긴다.
- 개발용 번호 대체(`dev_member`·`find_dev`): Playground처럼 표시 이름에 번호가 없는 사용자를 정한 번호로 본다.
  개발 환경(SEAM_ENV=development)에서만 켜지고, 운영에서 켜져 있으면 서버가 시작하지 않는다.
  번호는 개발용 명단 파일(roster.playground.json)에서만 찾고 팀 범위는 `squad:dev-<코드>`라서, 같은 번호가 실제 명단에 있어도
  실제 팀과 이어지지 않는다. 개발용 명단은 명단 표(roster)에 넣지 않는다.
"""

from __future__ import annotations

import os
import re
import uuid
from collections.abc import Callable
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import db

SCOPE_PREFIX = "squad:"
STALE_MINUTES = int(os.environ.get("TEAM_SETUP_STALE_MINUTES", "30"))
_NAME_RE = re.compile(r"^\s*User\s+(\d{1,12})\s*$", re.IGNORECASE)
_CODE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
DEV_CODE_PREFIX = "dev-"  # 개발용 팀 코드 앞머리. 실제 명단에는 쓸 수 없다


def member_no(display_name: str | None) -> str | None:
    """Teams 표시 이름에서 번호를 꺼낸다. 형식이 다르면 None(어느 팀에도 연결하지 않는다)."""
    found = _NAME_RE.match(display_name or "")
    return found.group(1) if found else None


def scope_of(team_code: str) -> str:
    return SCOPE_PREFIX + team_code


def load_roster(store: Path, rows: list[dict[str, Any]]) -> dict[str, int]:
    """명단을 넣는다(여러 번 넣어도 결과가 같다). rows: [{member_no, team_code, team_name, is_leader}].
    팀이 처음이면 등록부에 new로 만들고, 이미 있는 팀의 상태·연결 정보는 건드리지 않는다(이름만 맞춘다)."""
    db.initialize(store)
    now = db.now()
    teams_added = members = 0
    with db.write(store) as c:
        for row in rows:
            code = str(row["team_code"]).strip().lower()
            number = str(row["member_no"]).strip()
            if not _CODE_RE.match(code) or not number.isdigit():
                raise db.ValidationError("명단 형식이 올바르지 않아요(팀 코드는 영문 소문자·숫자, 번호는 숫자)")
            if code.startswith(DEV_CODE_PREFIX):
                raise db.ValidationError("dev-로 시작하는 팀 코드는 개발용이라 명단에 넣을 수 없어요")
            scope = scope_of(code)
            placed = c.execute("SELECT scope FROM roster WHERE member_no = ?", (number,)).fetchone()
            if placed is not None and placed[0] != scope:  # 다른 팀에 있는 번호를 옮기지 않는다(기존 팀 데이터 덮어쓰기 금지)
                raise db.ValidationError("이미 다른 팀에 있는 번호가 명단에 있어요. 옮기려면 먼저 확인해 주세요(아무것도 넣지 않았어요)")
            before = c.total_changes
            c.execute("INSERT OR IGNORE INTO teams(scope, name, status, updated_at) VALUES (?, ?, 'new', ?)",
                      (scope, str(row.get("team_name") or code), now))
            teams_added += c.total_changes - before
            c.execute("UPDATE teams SET name = ? WHERE scope = ? AND name != ?", (str(row.get("team_name") or code), scope,
                                                                                 str(row.get("team_name") or code)))
            c.execute("""INSERT INTO roster(member_no, scope, is_leader) VALUES (?, ?, ?)
                         ON CONFLICT(member_no) DO UPDATE SET scope = excluded.scope, is_leader = excluded.is_leader""",
                      (number, scope, 1 if row.get("is_leader") else 0))
            members += 1
    return {"teams_added": teams_added, "members": members}


def roster_active(store: Path) -> bool:
    """명단이 하나라도 있으면 명단으로 팀을 정한다."""
    db.initialize(store)
    with closing(db.connect(store)) as c:
        return c.execute("SELECT 1 FROM roster LIMIT 1").fetchone() is not None


def find(store: Path, display_name: str | None) -> dict[str, Any] | None:
    """표시 이름 → 팀(scope, name, status, is_leader). 번호가 없거나 명단에 없으면 None."""
    number = member_no(display_name)
    if number is None:
        return None
    db.initialize(store)
    with closing(db.connect(store)) as c:
        row = c.execute("""SELECT t.scope, t.name, t.status, r.is_leader FROM roster r JOIN teams t ON t.scope = r.scope
                           WHERE r.member_no = ?""", (number,)).fetchone()
    return dict(row) if row else None


def get(store: Path, scope: str) -> dict[str, Any] | None:
    db.initialize(store)
    with closing(db.connect(store)) as c:
        row = c.execute("SELECT * FROM teams WHERE scope = ?", (scope,)).fetchone()
        if row is None:
            return None
        team = dict(row)
        team["members"] = c.execute("SELECT COUNT(*) FROM roster WHERE scope = ?", (scope,)).fetchone()[0]
    return team


def is_stale(started_at: str | None, now: datetime | None = None) -> bool:
    """setting_up이 STALE_MINUTES 넘게 멈췄는지(그러면 다른 사람이 다시 시작할 수 있다)."""
    now = now or datetime.now(timezone.utc)
    if not started_at:
        return True
    try:
        started = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
    except ValueError:
        return True
    return now - started > timedelta(minutes=STALE_MINUTES)


def begin_setup(store: Path, scope: str, actor: str, now: datetime | None = None) -> str:
    """연결을 시작한다. 돌려주는 값: started(새로 시작·멈춘 것 이어받기) / resumed(내가 하던 것) / busy(다른 사람이 진행 중) /
    connected(이미 연결됨) / unknown(등록부에 없는 팀). 같은 사람이 여러 번 눌러도 결과가 같다."""
    moment = now or datetime.now(timezone.utc)
    with db.write(store) as c:
        row = c.execute("SELECT status, setup_by, setup_started_at FROM teams WHERE scope = ?", (scope,)).fetchone()
        if row is None:
            return "unknown"
        if row["status"] == "connected":
            return "connected"
        if row["status"] == "setting_up" and row["setup_by"] == actor:
            return "resumed"
        if row["status"] == "setting_up" and not is_stale(row["setup_started_at"], moment):
            return "busy"
        stamp = moment.isoformat().replace("+00:00", "Z")
        c.execute("""UPDATE teams SET status = 'setting_up', setup_by = ?, setup_started_at = ?, updated_at = ?
                     WHERE scope = ?""", (actor, stamp, stamp, scope))
        return "started"


def finish_setup(store: Path, scope: str, actor: str, write_config: Callable[[], None],
                 fields: tuple[str, ...] = ()) -> str:
    """연결을 끝낸다(설정 저장 + connected). 돌려주는 값: connected(이번에 끝냄) / already(이미 연결됨: 아무것도 쓰지 않는다) /
    lost(다른 사람이 이어받았거나 시작한 적이 없음: 쓰지 않는다). 확인과 저장을 같은 쓰기 잠금 안에서 해서, 동시에 끝내도
    먼저 들어온 한 명만 저장한다."""
    with db.write(store) as c:
        row = c.execute("SELECT status, setup_by FROM teams WHERE scope = ?", (scope,)).fetchone()
        if row is None:
            return "lost"
        if row["status"] == "connected":
            return "already"
        if row["status"] != "setting_up" or row["setup_by"] != actor:
            return "lost"
        write_config()
        now = db.now()
        c.execute("UPDATE teams SET status = 'connected', connected_at = ?, updated_at = ? WHERE scope = ?", (now, now, scope))
        for field in fields:
            log_change(c, scope, field, "set", actor, now)
        return "connected"


def log_change(c: Any, scope: str, field: str, action: str, actor: str, at: str | None = None) -> None:
    """설정 변경 기록. 값은 받지도 저장하지도 않는다."""
    c.execute("INSERT INTO team_config_log(id, scope, field, action, actor, changed_at) VALUES (?, ?, ?, ?, ?, ?)",
              (str(uuid.uuid4()), scope, field, action, actor, at or db.now()))


def record_changes(store: Path, scope: str, fields: tuple[str, ...], actor: str) -> None:
    with db.write(store) as c:
        for field in fields:
            log_change(c, scope, field, "set", actor)


def changes(store: Path, scope: str) -> list[dict[str, Any]]:
    db.initialize(store)
    with closing(db.connect(store)) as c:
        return [dict(r) for r in c.execute("SELECT field, action, actor, changed_at FROM team_config_log WHERE scope = ? ORDER BY changed_at",
                                           (scope,))]


# ── 개발용 번호 대체 ─────────────────────────────────────────

class DevOverrideError(RuntimeError):
    """개발용 번호 대체를 켤 수 없는 환경. 서버 시작을 거부한다(메시지에 번호를 넣지 않는다)."""


_LIVE_TEAMS_KEYS = ("CLIENT_ID", "CLIENT_SECRET", "TENANT_ID")


def dev_member(environ: Any) -> str | None:
    """SEAM_DEV_MEMBER_NO가 있으면 그 번호. 개발 환경이 아니면(SEAM_ENV != development, 또는 실제 Teams 자격증명이 있으면)
    DevOverrideError. 꺼져 있으면 None."""
    number = str(environ.get("SEAM_DEV_MEMBER_NO") or "").strip()
    if not number:
        return None
    if str(environ.get("SEAM_ENV") or "").strip().lower() != "development":
        raise DevOverrideError("SEAM_DEV_MEMBER_NO는 개발 환경(SEAM_ENV=development)에서만 쓸 수 있어요. 운영에서는 지워 주세요.")
    if any(environ.get(key) for key in _LIVE_TEAMS_KEYS):
        raise DevOverrideError("실제 Teams 자격증명(CLIENT_ID 등)이 있는 환경에서는 개발용 번호 대체를 켤 수 없어요.")
    if not number.isdigit():
        raise DevOverrideError("SEAM_DEV_MEMBER_NO는 숫자여야 해요.")
    return number


def dev_roster(path: Path, number: str) -> dict[str, Any]:
    """개발용 명단 파일에서 그 번호의 팀을 찾는다 → {"code": "dev-…", "name", "is_leader"}. 없으면 DevOverrideError."""
    import json

    try:
        rows = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise DevOverrideError(f"개발용 명단 파일을 읽지 못했어요({type(error).__name__}).") from None
    for row in rows if isinstance(rows, list) else []:
        if str(row.get("member_no") or "").strip() != number:
            continue
        code = DEV_CODE_PREFIX + str(row.get("team_code") or "").strip().lower()
        if not _CODE_RE.match(code):
            raise DevOverrideError("개발용 명단의 팀 코드 형식이 올바르지 않아요.")
        return {"code": code, "name": str(row.get("team_name") or code), "is_leader": bool(row.get("is_leader"))}
    raise DevOverrideError("SEAM_DEV_MEMBER_NO가 개발용 명단 파일에 없어요.")


def find_dev(store: Path, entry: dict[str, Any]) -> dict[str, Any]:
    """개발용 팀(squad:dev-…)을 등록부에 만들고(이미 있으면 그대로) find()와 같은 모양으로 돌려준다. 명단 표는 건드리지 않는다."""
    scope = scope_of(entry["code"])
    db.initialize(store)
    with db.write(store) as c:
        c.execute("INSERT OR IGNORE INTO teams(scope, name, status, updated_at) VALUES (?, ?, 'new', ?)",
                  (scope, entry["name"], db.now()))
        row = c.execute("SELECT scope, name, status FROM teams WHERE scope = ?", (scope,)).fetchone()
    return {**dict(row), "is_leader": 1 if entry["is_leader"] else 0}
