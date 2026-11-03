"""작업 한 번(메뉴 응답, 백그라운드 주기)마다 Notion·Groq 호출 수와 걸린 시간을 한 줄로 남긴다.

    metric kind=menu team=user:… menu=1 notion_calls=0 groq_calls=1 groq_tokens=812 parts=0 fallback=0 elapsed_ms=4061

호출 지점은 `count_notion()`, `count_groq()`만 부르면 된다. 지금 측정 중인 작업이 없으면 아무것도 하지 않는다.
측정은 contextvars로 따라간다. asyncio.to_thread는 문맥을 넘겨주지만 스레드 풀은 넘겨주지 않으므로,
풀에 일을 넘길 때는 `in_context()`로 감싼다. 토큰·비밀값은 남기지 않는다(팀 키와 숫자만).
"""

from __future__ import annotations

import contextvars
import logging
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any, TypeVar

log = logging.getLogger("metrics")

FIELDS = ("notion_calls", "groq_calls", "groq_tokens", "parts", "fallback")
T = TypeVar("T")


class Meter:
    def __init__(self) -> None:
        self.counts = dict.fromkeys(FIELDS, 0)
        self._lock = threading.Lock()

    def add(self, field: str, amount: int = 1) -> None:
        with self._lock:
            self.counts[field] += amount


_current: contextvars.ContextVar[Meter | None] = contextvars.ContextVar("metrics_meter", default=None)


def _add(field: str, amount: int = 1) -> None:
    meter = _current.get()
    if meter is not None:
        meter.add(field, amount)


def count_notion() -> None:
    _add("notion_calls")


def count_groq(usage: dict | None = None) -> None:
    """Groq 호출 1회. usage(응답의 usage 또는 x_groq.usage)가 있으면 토큰도 더한다."""
    _add("groq_calls")
    if usage:
        _add("groq_tokens", int(usage.get("total_tokens") or 0))


def format_line(kind: str, team: str, menu: str, counts: dict[str, int], elapsed_ms: int) -> str:
    fields = " ".join(f"{name}={counts[name]}" for name in FIELDS)
    return f"metric kind={kind} team={team or '-'} menu={menu or '-'} {fields} elapsed_ms={elapsed_ms}"


@contextmanager
def measure(kind: str, team: str = "", menu: str = "") -> Iterator[Meter]:
    """이 블록 안에서 일어난 호출을 세고, 끝날 때(예외여도) metric 한 줄을 남긴다."""
    meter = Meter()
    token = _current.set(meter)
    started = time.monotonic()
    try:
        yield meter
    finally:
        _current.reset(token)
        elapsed_ms = int((time.monotonic() - started) * 1000)
        log.info(format_line(kind, team, menu, meter.counts, elapsed_ms))


def in_context(fn: Callable[..., T]) -> Callable[..., T]:
    """스레드 풀에 넘길 함수를 지금 측정 문맥 안에서 돌게 감싼다."""
    ctx = contextvars.copy_context()

    def run(*args: Any, **kwargs: Any) -> T:
        # 같은 Context는 두 스레드가 동시에 들어갈 수 없다. 호출마다 복사본으로 돈다(Meter는 같은 것을 가리킨다).
        return ctx.copy().run(fn, *args, **kwargs)

    return run
