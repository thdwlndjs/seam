"""Groq 호출 순서와 한도. 문서: docs/v1.1/spec.md P1-8.

Groq 무료 등급 한도는 조직(키) 단위로 분당 약 8,000토큰, 하루 20만 토큰이다(gpt-oss-120b).
사용자 요청(메뉴 요약, 사람이 누른 [반영])과 백그라운드(종류 판정, 추출, 단계 판정)가 같은 한도를 나눠 쓴다.

| 순위 | 작업 | 규칙 |
| --- | --- | --- |
| 1 | 사람이 기다리는 것 | 막지 않는다. 쓴 양만 센다 |
| 2 | 종류 판정·추출(백그라운드) | 분당 남은 토큰이 모자라면 리셋까지 미룬다. 하루 합계가 한도의 70%를 넘으면 내일로 |
| 3 | 단계 판정(P2) | 2와 같다 |

- 분당 남은 양은 Groq 응답 헤더(`x-ratelimit-remaining-tokens`, `x-ratelimit-reset-tokens`)로 안다. 429면 `retry-after`까지.
- 하루 합계는 헤더로 오지 않아서 저장소(`groq_usage`, UTC 날짜별)에 직접 센다. Groq의 하루 경계와 정확히 같지는 않다.
"""

from __future__ import annotations

import os
import re
import threading
import time
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping

from worklog import db as wdb

DAILY_TOKENS = int(os.environ.get("GROQ_DAILY_TOKENS", "200000"))
BACKGROUND_SHARE = float(os.environ.get("GROQ_BACKGROUND_SHARE", "0.7"))  # [가정]

USER, BACKGROUND, STAGE = 1, 2, 3


class Busy(Exception):
    """백그라운드 호출을 지금 하지 않는다. reason: minute | daily. wait_seconds 뒤에 다시."""

    def __init__(self, reason: str, wait_seconds: float) -> None:
        super().__init__(f"groq busy ({reason}, {wait_seconds:.0f}s)")
        self.reason, self.wait_seconds = reason, wait_seconds


_lock = threading.Lock()
_minute: dict[str, float | None] = {"remaining": None, "reset_at": 0.0}


def parse_duration(text: str | None) -> float:
    """Groq 리셋 표기("7.66s", "1m2.5s", "120ms", "2h")를 초로."""
    if not text:
        return 0.0
    total = 0.0
    for value, unit in re.findall(r"([\d.]+)(ms|h|m|s)", text):
        total += float(value) * {"ms": 0.001, "s": 1, "m": 60, "h": 3600}[unit]
    return total


def _today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


def _seconds_to_tomorrow() -> float:
    now = datetime.now(UTC)
    return ((now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0) - now).total_seconds()


def daily_usage(store: Path) -> dict[str, int]:
    wdb.initialize(store)
    with closing(wdb.connect(store)) as c:
        row = c.execute("SELECT tokens, background_tokens FROM groq_usage WHERE day = ?", (_today(),)).fetchone()
    return {"tokens": row["tokens"], "background_tokens": row["background_tokens"]} if row else {"tokens": 0, "background_tokens": 0}


def check(priority: int, estimated_tokens: int, store: Path) -> None:
    """백그라운드(순위 2·3)가 지금 불러도 되는지. 안 되면 Busy. 사람이 기다리는 호출(1)은 막지 않는다."""
    if priority <= USER:
        return
    now = time.monotonic()
    with _lock:
        remaining, reset_at = _minute["remaining"], _minute["reset_at"] or 0.0
    if reset_at > now and remaining is not None and remaining < estimated_tokens:
        raise Busy("minute", reset_at - now)
    if daily_usage(store)["tokens"] + estimated_tokens > DAILY_TOKENS * BACKGROUND_SHARE:
        raise Busy("daily", _seconds_to_tomorrow())


def record(store: Path, priority: int, headers: Mapping[str, str] | None, status_code: int, tokens: int) -> None:
    """응답 하나를 반영한다: 분당 남은 양(헤더), 429면 retry-after까지 막음, 하루 합계에 토큰을 더한다."""
    headers = {k.lower(): v for k, v in (headers or {}).items()}
    now = time.monotonic()
    with _lock:
        if status_code == 429:
            wait = float(headers.get("retry-after") or 0) or parse_duration(headers.get("x-ratelimit-reset-tokens")) or 60.0
            _minute.update(remaining=0, reset_at=now + wait)
        elif "x-ratelimit-remaining-tokens" in headers:
            try:
                remaining = float(headers["x-ratelimit-remaining-tokens"])
            except ValueError:
                remaining = None
            _minute.update(remaining=remaining, reset_at=now + parse_duration(headers.get("x-ratelimit-reset-tokens")))
    if tokens:
        wdb.initialize(store)
        with wdb.write(store) as c:
            c.execute("""INSERT INTO groq_usage(day, tokens, background_tokens) VALUES (?, ?, ?)
                         ON CONFLICT(day) DO UPDATE SET tokens = tokens + excluded.tokens,
                             background_tokens = background_tokens + excluded.background_tokens""",
                      (_today(), tokens, tokens if priority > USER else 0))


def busy_message(error: Busy) -> str:
    """사용자에게 보일 안내."""
    if error.reason == "daily":
        return "오늘 정리할 몫을 다 썼어요. 내일 다시 정리돼요. 원문은 저장했어요."
    return f"{max(1, round(error.wait_seconds))}초 뒤 이어서 정리해요."


def reset_for_tests() -> None:
    with _lock:
        _minute.update(remaining=None, reset_at=0.0)


def response_tokens(body: Any) -> int:
    try:
        return int((body.get("usage") or {}).get("total_tokens") or 0)
    except AttributeError:
        return 0
