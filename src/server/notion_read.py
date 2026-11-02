"""Notion 페이지 읽기 도구. 기존 메뉴(1~3번)와 worklog 수집(notion_map.py)이 같이 쓴다.

main.py에 있던 함수를 그대로 옮겼다(동작 변경 없음). Notion은 읽기만 한다.
"""

from __future__ import annotations

import concurrent.futures
from collections.abc import Callable

from notion_client import Client


def _rich_text(block: dict) -> str:
    return "".join(item.get("plain_text", "") for item in block.get("rich_text", []))


def _map_parallel(fn: Callable[[dict], list[str]], items: list[dict]) -> list[list[str]]:
    """형제 블록/행끼리 병렬 처리. 호출마다 자기 풀을 만들고 닫아서 데드락이 없고, 순서는 보존된다."""
    if len(items) <= 1:
        return [fn(item) for item in items]
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(items)) as pool:
        return list(pool.map(fn, items))


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
