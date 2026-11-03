"""Notion 페이지·DB 행 본문을 md로 한 번에 받는다. 문서: docs/v1.1/spec.md P1-1.

블록 API로 하나씩 따라 읽으면 페이지 하나에 수십 번을 부르고 표 셀이 빠진다. md 엔드포인트
(`GET /v1/pages/{id}/markdown`, Notion-Version 2026-03-11)는 표·토글·동기화 블록까지 한 번에 준다.
클라이언트는 `notion_read.notion_client_for()`가 만든 것을 쓴다(호출 관문을 지나고 `page_markdown`이 있다).
md를 못 받으면(지원 안 하는 클라이언트, 오류, 이어 받기 상한 초과) `md=None`과 이유를 돌려주고,
호출부는 예전 블록 읽기로 대신한다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import metrics

log = logging.getLogger("notion_md")

MAX_CALLS = 20  # 페이지 하나에 md 엔드포인트를 부르는 최대 횟수(처음 1번 + 이어 받기)


@dataclass
class MarkdownPage:
    page_id: str
    md: str | None
    fallback_reason: str | None = None  # unsupported | error | too_many_calls


def fetch_markdown(client: Any, page_id: str, max_calls: int = MAX_CALLS) -> MarkdownPage:
    """페이지 md. 잘린 응답은 unknown_block_ids로 이어 받아 순서대로 붙인다."""
    get = getattr(client, "page_markdown", None)
    if get is None:
        return _fallback(page_id, "unsupported")
    pieces: list[str] = []
    pending = [page_id]
    calls = 0
    while pending:
        if calls >= max_calls:
            return _fallback(page_id, "too_many_calls")
        block_id = pending.pop(0)
        calls += 1
        try:
            answer = get(block_id)
        except Exception as error:  # 404(권한 없음·없는 페이지), 지원 안 되는 블록 등
            log.info("md 실패 %s: %s", block_id, error)
            return _fallback(page_id, "error")
        pieces.append(answer.get("markdown") or "")
        if answer.get("truncated"):
            pending = list(answer.get("unknown_block_ids") or []) + pending
    return MarkdownPage(page_id, "\n".join(pieces))


def _fallback(page_id: str, reason: str) -> MarkdownPage:
    if reason != "unsupported":
        metrics.count("fallback")
    return MarkdownPage(page_id, None, reason)
