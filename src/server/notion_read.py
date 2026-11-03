"""Notion 페이지 읽기 도구. 기존 메뉴(1~3번)와 worklog 수집(notion_map.py)이 같이 쓴다. Notion은 읽기만 한다.

Notion API 한도는 통합 토큰마다 평균 초당 약 3회다. 메뉴 응답·설정 검증·백그라운드 수집이 각자 요청을
쏘면 한도에 걸리고, notion-client가 429를 받아 기다렸다 재시도하느라 응답이 수십 초씩 늦어진다.
그래서 모든 Notion 호출은 `notion_client_for()`가 만든 클라이언트로만 하고, 이 클라이언트는 토큰별 관문을 지난다.

- 토큰별로 초당 NOTION_RATE(3)회, 동시에 NOTION_CONCURRENCY(3)개까지만 나간다.
- 백그라운드 수집은 그 안에서 초당 NOTION_BACKGROUND_RATE(1)회까지만 쓴다. 나머지는 사용자 요청 몫이다.
"""

from __future__ import annotations

import concurrent.futures
import inspect
import os
import threading
import time
from collections.abc import Callable
from typing import Any

from notion_client import Client

import metrics

NOTION_RATE = float(os.environ.get("NOTION_RATE", "3"))
NOTION_BACKGROUND_RATE = float(os.environ.get("NOTION_BACKGROUND_RATE", "1"))
NOTION_CONCURRENCY = int(os.environ.get("NOTION_CONCURRENCY", "3"))
# 한 블록의 자식들을 동시에 읽는 개수. 관문보다 많이 띄워 봐야 기다리기만 한다.
PARALLEL_READS = int(os.environ.get("NOTION_PARALLEL_READS", "3"))


class _RateLimiter:
    """초당 rate회(최대 burst회 몰아서)까지 통과시키는 토큰 버킷. 스레드 안전."""

    def __init__(self, rate: float, burst: float | None = None) -> None:
        self.rate = rate
        self.capacity = burst if burst is not None else max(1.0, rate)
        self.tokens = self.capacity
        self.updated = time.monotonic()
        self.lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self.lock:
                now = time.monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
                self.updated = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                wait = (1 - self.tokens) / self.rate
            time.sleep(wait)


class _Gate:
    """토큰 하나의 관문: 전체 속도 + 동시 개수 + 백그라운드 몫."""

    def __init__(self) -> None:
        self.all = _RateLimiter(NOTION_RATE)
        # 백그라운드는 몰아 쏘지 못하게(burst 1) 고르게만 나간다. 그래야 사용자 요청 몫이 항상 남는다.
        self.background = _RateLimiter(NOTION_BACKGROUND_RATE, burst=1)
        self.slots = threading.BoundedSemaphore(NOTION_CONCURRENCY)

    def call(self, fn: Callable[..., Any], background: bool, *args: Any, **kwargs: Any) -> Any:
        if background:
            self.background.acquire()
        self.all.acquire()
        with self.slots:
            metrics.count_notion()
            return fn(*args, **kwargs)


_gates: dict[str, _Gate] = {}
_gates_lock = threading.Lock()


def _gate_for(token: str) -> _Gate:
    with _gates_lock:
        return _gates.setdefault(token, _Gate())


class _Throttled:
    """Notion 클라이언트(와 그 하위 endpoint)를 감싸서, API 메서드 호출만 관문을 지나게 한다."""

    def __init__(self, target: Any, gate: _Gate, background: bool) -> None:
        self._target, self._gate, self._background = target, gate, background

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._target, name)
        if inspect.ismethod(attr) or inspect.isfunction(attr):
            def call(*args: Any, **kwargs: Any) -> Any:
                return self._gate.call(attr, self._background, *args, **kwargs)
            return call
        if attr is None or isinstance(attr, (str, int, float, bool, dict, list, tuple)):
            return attr
        return _Throttled(attr, self._gate, self._background)


def notion_client_for(token: str, background: bool = False) -> Any:
    """Notion 호출은 항상 이걸로 만든 클라이언트로 한다. background=True는 수집용(사용자 요청에 자리를 양보)."""
    return _Throttled(Client(auth=token), _gate_for(token), background)


def _rich_text(block: dict) -> str:
    return "".join(item.get("plain_text", "") for item in block.get("rich_text", []))


def _map_parallel(fn: Callable[[dict], list[str]], items: list[dict]) -> list[list[str]]:
    """형제 블록/행끼리 병렬 처리. 호출마다 자기 풀을 만들고 닫고, 순서는 보존된다."""
    if len(items) <= 1:
        return [fn(item) for item in items]
    # 재귀 호출마다 풀이 생기므로 동시 스레드는 단계마다 곱해진다. 단계당 개수를 묶어 두고, 실제 API 호출 수는
    # 관문(_Gate)이 토큰 단위로 다시 묶는다. 관문은 API 호출 순간만 잡으므로 재귀 대기와 얽혀 막히지 않는다.
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(items), PARALLEL_READS)) as pool:
        return list(pool.map(metrics.in_context(fn), items))


def _list_child_blocks(block_id: str, notion_client: Client) -> list[dict]:
    blocks: list[dict] = []
    cursor: str | None = None
    while True:
        response = notion_client.blocks.children.list(block_id, start_cursor=cursor)
        blocks.extend(response["results"])
        if not response.get("has_more"):
            return blocks
        cursor = response.get("next_cursor")


def _query_database_rows(database_id: str, notion_client: Client) -> list[dict]:
    """Notion API 2025-09부터는 데이터베이스 안의 data source를 조회한다."""
    db = notion_client.databases.retrieve(database_id)
    data_source_id = db["data_sources"][0]["id"]

    rows: list[dict] = []
    cursor: str | None = None
    while True:
        response = notion_client.data_sources.query(data_source_id, start_cursor=cursor)
        rows.extend(response["results"])
        if not response.get("has_more"):
            return rows
        cursor = response.get("next_cursor")


def _inline_database_lines(database_id: str, notion_client: Client, depth: int) -> list[str]:
    def _row_lines(row: dict) -> list[str]:
        row_lines: list[str] = []
        title = ""
        for prop in row.get("properties", {}).values():
            if prop.get("type") == "title":
                title = "".join(t.get("plain_text", "") for t in prop.get("title", []))
                break
        if title:
            row_lines.append("  " * depth + title)
        for child in _list_child_blocks(row["id"], notion_client):
            row_lines.extend(_block_text(child, notion_client, depth + 1))
        return row_lines

    rows = _query_database_rows(database_id, notion_client)
    lines: list[str] = []
    for row_lines in _map_parallel(_row_lines, rows):
        lines.extend(row_lines)
    return lines


def _block_text(block: dict, notion_client: Client, depth: int = 0) -> list[str]:
    block_type = block["type"]
    data = block.get(block_type, {})
    indent = "  " * depth

    if block_type == "child_database":
        return _inline_database_lines(block["id"], notion_client, depth)

    if block_type == "child_page":
        # 다른 문서로 가는 바로가기 — 안으로 들어가지 않고 제목만 남긴다.
        title = block.get("child_page", {}).get("title", "")
        return [indent + f"📄 {title}"] if title else []

    if block_type in {"bulleted_list_item", "numbered_list_item"}:
        prefix = "- " if block_type == "bulleted_list_item" else "1. "
        text = prefix + _rich_text(data)
    elif block_type == "to_do":
        checked = "[x]" if data.get("checked") else "[ ]"
        text = f"{checked} {_rich_text(data)}"
    elif block_type == "divider":
        text = "---"
    else:
        text = _rich_text(data)

    lines = [indent + text] if text else []
    if block.get("has_children"):
        children = _list_child_blocks(block["id"], notion_client)
        for child_lines in _map_parallel(lambda c: _block_text(c, notion_client, depth + 1), children):
            lines.extend(child_lines)
    return lines


def _fetch_notion_page_by_id(page_id: str, notion_client: Client) -> str:
    page = notion_client.pages.retrieve(page_id)
    blocks = _list_child_blocks(page_id, notion_client)
    lines: list[str] = []
    for block_lines in _map_parallel(lambda b: _block_text(b, notion_client), blocks):
        lines.extend(block_lines)

    title = ""
    for property_data in page.get("properties", {}).values():
        if property_data.get("type") == "title":
            title = "".join(item.get("plain_text", "") for item in property_data.get("title", []))
            break

    return "\n".join(([title] if title else []) + lines) or "Notion 페이지에 표시할 내용이 없습니다."
