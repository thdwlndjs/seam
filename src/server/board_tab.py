"""구조 보기·고치기 탭: 입장 링크(서명된 토큰)와 화면·API 경로. 문서: docs/v1.1/spec.md P2-8.

- 봇 메인 카드의 [구조 보기·고치기] 버튼이 그 사람·그 팀 전용 링크를 연다. 링크의 토큰은 서버 비밀값으로 서명하고
  BOARD_TOKEN_HOURS 뒤 만료된다. Teams 로그인(SSO)을 붙이기 전의 입장 방식이다.
- Teams 탭으로 열 때는 딥 링크의 subEntityId에 토큰을 넣는다(TEAMS_APP_ID가 있을 때). 없으면 웹 주소를 연다(Playground).
- 고치기는 권한자만 한다(can_edit). 보는 것은 팀원 누구나.
- 이 서버는 바깥에 직접 열지 않는다. 봇(3978)이 /tab/·/api/board 요청을 이 서버로 넘긴다.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import time
import urllib.parse
from collections.abc import Callable
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from worklog import board
from worklog.db import ConflictError, NotFoundError, ValidationError

TOKEN_HOURS = float(os.environ.get("BOARD_TOKEN_HOURS", "12"))
PAGE = Path(__file__).resolve().parent / "tab" / "board.html"
ENTITY_ID = "board"


def _key(secret: str) -> bytes:
    return hashlib.sha256(b"board-tab:" + secret.encode()).digest()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def issue(secret: str, user_id: str, name: str | None, scope: str, now: float | None = None, view: str = "") -> str:
    claims = {"u": user_id, "n": name or "", "s": scope, "e": int((now or time.time()) + TOKEN_HOURS * 3600)}
    if view:  # 열자마자 보일 화면(예: "stages" = 단계 상세)
        claims["w"] = view
    body = _b64(json.dumps(claims, ensure_ascii=False).encode())
    return body + "." + _b64(hmac.new(_key(secret), body.encode(), hashlib.sha256).digest())


def verify(secret: str, token: str | None, now: float | None = None) -> dict:
    """토큰 → {u, n, s}. 서명이 틀리거나 만료면 401."""
    try:
        body, sig = (token or "").split(".", 1)
        expected = _b64(hmac.new(_key(secret), body.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, expected):
            raise ValueError
        claims = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    except ValueError:
        raise HTTPException(status_code=401, detail="링크가 올바르지 않아요. 봇 카드에서 다시 열어 주세요.") from None
    if claims["e"] < (now or time.time()):
        raise HTTPException(status_code=401, detail="링크가 만료됐어요. 봇 카드에서 다시 열어 주세요.")
    return claims


def link(secret: str, user_id: str, name: str | None, scope: str, view: str = "") -> str:
    """메인 카드 버튼이 여는 주소. view가 있으면 그 화면(단계 상세 등)을 바로 연다."""
    token = issue(secret, user_id, name, scope, view=view)
    app_id = os.environ.get("TEAMS_APP_ID")
    if app_id:
        context = json.dumps({"subEntityId": token}, separators=(",", ":"))
        return (f"https://teams.microsoft.com/l/entity/{app_id}/{ENTITY_ID}?"
                + urllib.parse.urlencode({"context": context}))
    base = os.environ.get("BOARD_PUBLIC_URL", "http://localhost:3978").rstrip("/")
    return f"{base}/tab/board?t={urllib.parse.quote(token)}"


class EditBody(BaseModel):
    kind: str | None = None
    title: str | None = None
    assignee: str | None = None
    statement: str | None = None


class AnswerBody(BaseModel):
    answer: str


def register(app: FastAPI, secret: str, store: Callable[[], Path], can_edit: Callable[[str, str], bool]) -> None:
    """탭 화면과 API 경로를 붙인다. can_edit(scope, user_id): 고치기 권한."""

    def viewer(authorization: str | None) -> dict:
        token = (authorization or "").removeprefix("Bearer ").strip()
        return verify(secret, token)

    def editor(authorization: str | None) -> dict:
        claims = viewer(authorization)
        if not can_edit(claims["s"], claims["u"]):
            raise HTTPException(status_code=403, detail="고치기는 권한자(관리자·개발·권한을 받은 사람)가 해요.")
        return claims

    async def run(fn: Callable, *args):
        try:
            return await asyncio.to_thread(fn, *args)
        except NotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from None
        except (ConflictError, ValidationError) as error:
            raise HTTPException(status_code=409, detail=str(error)) from None

    @app.get("/tab/board")
    async def board_page():
        return FileResponse(PAGE, media_type="text/html; charset=utf-8", headers={"Cache-Control": "no-store"})

    @app.get("/api/board")
    async def board_data(authorization: str | None = Header(None)):
        claims = viewer(authorization)
        data = await run(board.snapshot, store(), claims["s"])
        data["viewer"] = {"name": claims["n"], "can_edit": can_edit(claims["s"], claims["u"]), "view": claims.get("w", "")}
        return data

    def actor(claims: dict) -> str:
        return claims["n"] or claims["u"]

    @app.post("/api/board/items/{item_id}")
    async def board_edit(item_id: str, body: EditBody, authorization: str | None = Header(None)):
        claims = editor(authorization)
        changes = body.model_dump(exclude_none=True)
        return await run(board.edit, store(), claims["s"], item_id, changes, actor(claims))

    @app.post("/api/board/items/{item_id}/cancel")
    async def board_cancel(item_id: str, authorization: str | None = Header(None)):
        claims = editor(authorization)
        return await run(board.cancel, store(), claims["s"], item_id, actor(claims))

    @app.post("/api/board/items/{item_id}/answer")
    async def board_answer(item_id: str, body: AnswerBody, authorization: str | None = Header(None)):
        claims = editor(authorization)
        return await run(board.resolve, store(), claims["s"], item_id, body.answer, actor(claims))

    @app.post("/api/board/changes/{candidate_id}/revert")
    async def board_revert(candidate_id: str, authorization: str | None = Header(None)):
        claims = editor(authorization)
        return await run(board.revert_change, store(), claims["s"], candidate_id, actor(claims))
