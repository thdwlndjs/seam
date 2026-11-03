"""LLM 호출 순서와 한도, 공급자 예비. 문서: docs/v1.1/spec.md P1-8.

같은 모델(gpt-oss-120b)을 두 공급자에서 쓴다. 백그라운드 작업은 앞 공급자가 막히면 다음 공급자로 넘긴다.

| 공급자 | 무료 한도(gpt-oss-120b) | 남은 양을 아는 방법 |
| --- | --- | --- |
| groq | 분당 약 8,000토큰, 하루 20만 토큰(조직 단위) | 응답 헤더 x-ratelimit-remaining-tokens / -reset-tokens |
| cerebras | 분당 5회, 분당 9만 토큰(캐시 안 된 3만), 하루 100만 토큰 | 헤더 없음: 호출 횟수·하루 합계를 직접 센다 |

| 순위 | 작업 | 규칙 |
| --- | --- | --- |
| 1 | 사람이 기다리는 것(메뉴 요약) | 막지 않는다. 쓴 양만 센다. Groq만 쓴다 |
| 2 | 종류 판정·추출(백그라운드) | 공급자마다 분당·하루 몫을 넘으면 다음 공급자로. 다 막히면 다음 주기로 |
| 3 | 단계 판정(P2) | 2와 같다 |

- 하루 합계는 저장소 `llm_usage`(UTC 날짜·공급자별)에 센다. 공급자의 하루 경계와 정확히 같지는 않다.
- 키가 없는 공급자는 쓰지 않는다. CEREBRAS_API_KEY가 없으면 지금처럼 Groq만 쓴다.
- 같은 공급자의 키를 여러 개 돌려 한도를 넘기지 않는다(Groq 이용정책). 공급자는 공급자마다 하나씩이다.
"""

from __future__ import annotations

import os
import re
import threading
import time
from collections import deque
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping

from worklog import db as wdb

USER, BACKGROUND, STAGE = 1, 2, 3


@dataclass(frozen=True)
class Provider:
    name: str
    url: str
    key_env: str
    model: str
    daily_tokens: int
    background_share: float  # 하루 몫 중 백그라운드가 쓸 수 있는 비율
    rpm: int | None  # 분당 호출 수 상한(헤더가 없는 공급자용)

    @property
    def key(self) -> str | None:
        return os.environ.get(self.key_env) or None


def _providers() -> list[Provider]:
    groq_model = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")
    return [
        Provider("groq", "https://api.groq.com/openai/v1/chat/completions", "GROQ_API_KEY",
                 os.environ.get("GROQ_BACKGROUND_MODEL") or groq_model,
                 int(os.environ.get("GROQ_DAILY_TOKENS", "200000")),
                 float(os.environ.get("GROQ_BACKGROUND_SHARE", "0.7")), None),  # [가정] 70%
        Provider("cerebras", "https://api.cerebras.ai/v1/chat/completions", "CEREBRAS_API_KEY",
                 os.environ.get("CEREBRAS_MODEL", "gpt-oss-120b"),
                 int(os.environ.get("CEREBRAS_DAILY_TOKENS", "1000000")),
                 float(os.environ.get("CEREBRAS_BACKGROUND_SHARE", "0.9")),
                 int(os.environ.get("CEREBRAS_RPM", "5"))),
    ]


PROVIDERS = _providers()


def available(priority: int = BACKGROUND) -> list[Provider]:
    """키가 있는 공급자, 쓸 순서대로. 사람이 기다리는 호출은 Groq만 쓴다."""
    usable = [p for p in PROVIDERS if p.key]
    return usable[:1] if priority <= USER else usable


class Busy(Exception):
    """지금 이 공급자(또는 모든 공급자)를 부르지 않는다. reason: minute | daily. wait_seconds 뒤에 다시."""

    def __init__(self, reason: str, wait_seconds: float, provider: str = "") -> None:
        super().__init__(f"llm busy ({provider or 'all'}, {reason}, {wait_seconds:.0f}s)")
        self.reason, self.wait_seconds, self.provider = reason, wait_seconds, provider


_lock = threading.Lock()
_minute: dict[str, dict[str, float | None]] = {}
_calls: dict[str, deque[float]] = {}


def parse_duration(text: str | None) -> float:
    """리셋 표기("7.66s", "1m2.5s", "120ms", "2h")를 초로."""
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


def daily_usage(store: Path, provider: str = "groq") -> dict[str, int]:
    wdb.initialize(store)
    with closing(wdb.connect(store)) as c:
        row = c.execute("SELECT tokens, background_tokens FROM llm_usage WHERE day = ? AND provider = ?",
                        (_today(), provider)).fetchone()
    return {"tokens": row["tokens"], "background_tokens": row["background_tokens"]} if row else {"tokens": 0, "background_tokens": 0}


def check(provider: Provider, priority: int, estimated_tokens: int, store: Path) -> None:
    """이 공급자를 지금 불러도 되는지. 안 되면 Busy. 사람이 기다리는 호출(1)은 막지 않는다."""
    if priority <= USER:
        return
    now = time.monotonic()
    with _lock:
        state = _minute.get(provider.name, {})
        remaining, reset_at = state.get("remaining"), state.get("reset_at") or 0.0
        recent = _calls.setdefault(provider.name, deque())
        while recent and now - recent[0] >= 60:
            recent.popleft()
        calls_in_minute, oldest = len(recent), (recent[0] if recent else now)
    if reset_at > now and (remaining is None or remaining < estimated_tokens):
        raise Busy("minute", reset_at - now, provider.name)
    if provider.rpm and calls_in_minute >= provider.rpm:
        raise Busy("minute", 60 - (now - oldest), provider.name)
    if daily_usage(store, provider.name)["tokens"] + estimated_tokens > provider.daily_tokens * provider.background_share:
        raise Busy("daily", _seconds_to_tomorrow(), provider.name)


def check_any(priority: int, estimated_tokens: int, store: Path) -> Provider:
    """지금 부를 수 있는 첫 공급자. 모두 막혔으면 Busy(모두 하루 몫이면 daily, 아니면 가장 빨리 풀리는 대기)."""
    errors: list[Busy] = []
    for provider in available(priority):
        try:
            check(provider, priority, estimated_tokens, store)
            return provider
        except Busy as error:
            errors.append(error)
    if not errors:
        raise Busy("daily", _seconds_to_tomorrow())  # 키가 있는 공급자가 없다
    minute = [e for e in errors if e.reason == "minute"]
    raise min(minute, key=lambda e: e.wait_seconds) if minute else errors[0]


def record(store: Path, provider: str, priority: int, headers: Mapping[str, str] | None, status_code: int,
           tokens: int) -> None:
    """응답 하나를 반영한다: 분당 남은 양(헤더가 있으면), 429면 retry-after까지 막음, 호출 횟수·하루 합계."""
    headers = {k.lower(): v for k, v in (headers or {}).items()}
    now = time.monotonic()
    with _lock:
        _calls.setdefault(provider, deque()).append(now)
        if status_code == 429:
            wait = float(headers.get("retry-after") or 0) or parse_duration(headers.get("x-ratelimit-reset-tokens")) or 60.0
            _minute[provider] = {"remaining": 0, "reset_at": now + wait}
        elif "x-ratelimit-remaining-tokens-minute" in headers:
            # Cerebras: 분·시간·하루 남은 양을 따로 준다(리셋 시각은 없음). 분 단위는 길어야 1분 뒤 풀린다.
            try:
                minute_left = float(headers["x-ratelimit-remaining-tokens-minute"])
                requests_left = [float(headers[h]) for h in ("x-ratelimit-remaining-requests-minute",
                                                             "x-ratelimit-remaining-requests-hour",
                                                             "x-ratelimit-remaining-requests-day") if h in headers]
            except ValueError:
                minute_left, requests_left = None, []
            hour_or_day_out = any(left <= 0 for left in requests_left[1:])
            _minute[provider] = {"remaining": 0 if hour_or_day_out or (requests_left and requests_left[0] <= 0) else minute_left,
                                 "reset_at": now + (3600 if hour_or_day_out else 60)}
        elif "x-ratelimit-remaining-tokens" in headers:
            try:
                remaining = float(headers["x-ratelimit-remaining-tokens"])
            except ValueError:
                remaining = None
            _minute[provider] = {"remaining": remaining,
                                 "reset_at": now + parse_duration(headers.get("x-ratelimit-reset-tokens"))}
    if tokens:
        wdb.initialize(store)
        with wdb.write(store) as c:
            c.execute("""INSERT INTO llm_usage(day, provider, tokens, background_tokens) VALUES (?, ?, ?, ?)
                         ON CONFLICT(day, provider) DO UPDATE SET tokens = tokens + excluded.tokens,
                             background_tokens = background_tokens + excluded.background_tokens""",
                      (_today(), provider, tokens, tokens if priority > USER else 0))


def busy_message(error: Busy) -> str:
    """사용자에게 보일 안내."""
    if error.reason == "daily":
        return "오늘 정리할 몫을 다 썼어요. 내일 다시 정리돼요. 원문은 저장했어요."
    return f"{max(1, round(error.wait_seconds))}초 뒤 이어서 정리해요."


def reset_for_tests() -> None:
    with _lock:
        _minute.clear()
        _calls.clear()
