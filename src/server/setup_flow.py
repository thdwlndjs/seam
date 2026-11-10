"""처음 온 팀원: 팀 연결 흐름(시나리오 A, 토스식 — 카드 하나에 질문 하나). 문서: docs/seam/01-setup.md.

순서: 팀 인사 → 노션 연결(링크+토큰, 확인되면 바로 팀 연결 저장) → GitHub 연결(선택) → 작업 보드(없으면 만들기) → 준비 끝.
시안의 '이 팀이 맞나요?'(프로젝트·목표·기간) 화면은 뺐다: 지금 코드로는 세 칸을 채울 수 없다(docs/seam/01-setup.md 결정 기록).

- 누가 어느 팀인지는 worklog/teams.py가 정한다(명단). 이 파일은 그 팀이 아직 연결 전일 때의 카드만 다룬다.
- 토큰·링크 검사는 카드 제출을 받는 `_check`(이 파일) 한 군데서만 한다: 형식 → 실제 접근 확인(GitHub·Notion 호출은 호출부가 넘긴 함수).
- 토큰 카드는 Adaptive Card 1.5 `Input.Text` style "Password"이고, 제출을 받으면 그 카드를 값 없는 완료 카드로 바꾼다(`turn.replace`).
- 토큰·번호는 카드 문구·오류 메시지·로그에 넣지 않는다. 오류 문구는 입력값을 되풀이하지 않는다.
- 같은 제출이 두 번 와도 결과가 같다: 연결 저장은 teams.finish_setup이 한 번만 하고, 이미 있는 설정 값은 덮어쓰지 않는다.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from worklog import teams

log = logging.getLogger("setup")

SETUP_ACTIONS = {"setup_start", "setup_notion", "setup_help", "setup_github_open",
                 "setup_github", "setup_github_later", "setup_board_create", "setup_board_skip"}
_NOTION_TOKEN_RE = re.compile(r"^(ntn_|secret_)\S{10,}$")
_GITHUB_TOKEN_RE = re.compile(r"^(ghp_|github_pat_)\S{10,}$")
_GUIDES = {
    "notion": ("노션 토큰 만드는 법\n"
               "1. notion.com/developers 에서 '새 연결'을 만들어요\n"
               "2. 읽기 권한을 체크하고 토큰을 복사해요(ntn_로 시작)\n"
               "3. 팀 노션 페이지 오른쪽 위 ··· → 연결 에 방금 만든 연결을 추가해요\n"
               "그다음 위 카드에 다시 붙여 넣어 주세요."),
    "github": ("GitHub 토큰 만드는 법\n"
               "1. github.com/settings/tokens → Generate new token (classic)\n"
               "2. repo, project 두 개만 체크해요\n"
               "3. 만든 ghp_ 토큰을 복사해요\n"
               "그다음 위 카드에 다시 붙여 넣어 주세요."),
}

# 팀 범위별 잠금(GitHub 연결·보드 만들기를 한 번만)
_scope_locks: dict[str, asyncio.Lock] = {}


@dataclass
class Deps:
    """바깥 세계(설정 파일·GitHub·Notion)와 닿는 함수들. main.py가 채운다."""
    store: Callable[[], Path]
    get_config: Callable[[str], dict | None]
    save_config: Callable[[str, dict], bool]  # (scope, 새 값) → 저장했는지. 이미 있는 키는 덮어쓰지 않는다
    parse_notion_page: Callable[[str], str | None]
    parse_github_repo: Callable[[str], str | None]
    notion_page_title: Callable[[str, str], str | None]  # (토큰, 페이지 ID) → 제목(못 열면 None). 스레드에서 부른다
    github_token_ok: Callable[[str], Awaitable[bool]]
    github_repo_ok: Callable[[str, str], Awaitable[bool]]
    find_board: Callable[[str, str], Awaitable[dict | None]]  # (repo, token) → {"id", "number"} | None
    create_board: Callable[[str, str, str], Awaitable[dict | None]]  # (repo, token, 제목)
    after_connect: Callable[[str, str, str], None]  # (scope, 페이지 ID, 토큰) → 백그라운드로 노션 읽기 시작


# ── 카드 ───────────────────────────────────────────────────

async def _info(turn: Any, heading: str, text: str, buttons: list[dict]) -> None:
    await turn.send_buttons(heading, buttons, card={"heading": heading, "text": text})


async def _notion_card(turn: Any, error: str | None = None) -> None:
    text = "페이지 링크와 연결 토큰 두 가지만 있으면 돼요."
    if error:
        text = f"⚠️ {error}\n{text}"
    await turn.send_buttons("1 / 2 · 노션", [
        {"title": "연결하기", "data": {"action": "setup_notion"}},
        {"title": "토큰이 뭐예요?", "data": {"action": "setup_help", "kind": "notion"}},
    ], card={"heading": "팀 노션 페이지를 알려 주세요", "text": text, "inputs": [
        {"type": "text", "id": "notion_link", "label": "페이지 링크", "placeholder": "https://www.notion.so/…"},
        {"type": "text", "id": "notion_token", "label": "연결 토큰", "style": "Password", "placeholder": "ntn_로 시작해요"},
    ]})


async def _github_card(turn: Any, error: str | None = None) -> None:
    text = "저장소 주소와 GitHub 토큰이 필요해요."
    if error:
        text = f"⚠️ {error}\n{text}"
    await turn.send_buttons("2 / 2 · GitHub", [
        {"title": "연결하기", "data": {"action": "setup_github"}},
        {"title": "토큰이 뭐예요?", "data": {"action": "setup_help", "kind": "github"}},
        {"title": "다음에 할게요", "data": {"action": "setup_github_later"}},
    ], card={"heading": "팀 저장소를 알려 주세요", "text": text, "inputs": [
        {"type": "text", "id": "github_repo", "label": "저장소 주소", "placeholder": "https://github.com/팀/저장소"},
        {"type": "text", "id": "github_token", "label": "GitHub 토큰", "style": "Password", "placeholder": "ghp_로 시작해요"},
    ]})


async def _done_card(turn: Any, team: dict, config: dict | None) -> None:
    config = config or {}
    lines = ["✓ 노션 연결됨", f"✓ {team['name']} 확인됨"]
    lines.append("✓ GitHub 연결됨" if config.get("github_repo") else "– GitHub는 나중에 (팀 설정에서 연결할 수 있어요)")
    if config.get("github_repo"):
        lines.append("✓ 작업 보드 연결됨" if config.get("github_project_number") else "– 작업 보드 없음")
    lines.append("팀원들은 들어오면 역할만 고르면 돼요.")
    await _info(turn, "준비 끝! 🎉", "\n".join(lines), [{"title": "팀 현황 먼저 보기", "data": {"action": "menu", "choice": "2"}}])


async def intro(turn: Any, team: dict) -> None:
    """연결 전인 팀의 팀원이 봇을 열거나 무엇을 누르든 처음 보는 카드. 번호는 보이지 않는다."""
    current = await asyncio.to_thread(teams.get, turn.setup_deps.store(), team["scope"]) or {}
    if current.get("status") == "setting_up" and current.get("setup_by") != turn.aad_id and not teams.is_stale(
            current.get("setup_started_at")):
        await turn.send("다른 팀원이 지금 팀을 연결하고 있어요. 끝나면 바로 쓸 수 있어요.\n"
                        f"{teams.STALE_MINUTES}분 넘게 멈춰 있으면 다시 시작할 수 있어요.")
        return
    mine = current.get("status") == "setting_up" and current.get("setup_by") == turn.aad_id
    await _info(turn, f"{team['name']}이시네요 👋",
                "팀 연결을 이어서 할게요." if mine else "아직 팀이 연결되지 않았어요. 노션만 연결하면 바로 시작할 수 있어요.",
                [{"title": "이어서 하기" if mine else "팀 연결 시작하기", "data": {"action": "setup_start"}}])


def blocked_text() -> str:
    """명단에 없는 사람. 이름·번호를 되풀이하지 않는다."""
    return ("어느 팀인지 찾지 못했어요.\nTeams 이름에서 팀 명단의 번호를 찾을 수 없어요. "
            "팀 명단에 있는 계정으로 들어왔는지 확인하고, 그래도 안 되면 운영진에게 알려 주세요.")


# ── 검사(한 군데) ───────────────────────────────────────────

async def _check(kind: str, value: dict, deps: Deps) -> tuple[dict | None, str | None]:
    """카드 제출 값 검사: 형식 → 실제 접근 확인. 통과하면 (깨끗한 값, None), 아니면 (None, 오류 문구). 오류 문구에 입력값을 넣지 않는다."""
    if kind == "notion":
        page_id = deps.parse_notion_page(str(value.get("notion_link") or ""))
        token = str(value.get("notion_token") or "").strip()
        if page_id is None:
            return None, "노션 페이지 링크를 붙여 주세요."
        if not token.isascii() or not _NOTION_TOKEN_RE.match(token):
            return None, "토큰 형식이 아니에요. ntn_로 시작하는 토큰을 공백 없이 붙여 주세요."
        title = await asyncio.to_thread(deps.notion_page_title, token, page_id)
        if title is None:
            return None, "이 페이지를 열 수 없어요. 토큰이 맞는지, 페이지 ··· → 연결에 추가했는지 확인해 주세요."
        return {"notion_token": token, "notion_page_id": page_id, "page_title": title}, None
    repo = deps.parse_github_repo(str(value.get("github_repo") or ""))
    token = str(value.get("github_token") or "").strip()
    if repo is None:
        return None, "저장소 주소를 붙여 주세요. (예: https://github.com/팀/저장소)"
    if not token.isascii() or not _GITHUB_TOKEN_RE.match(token):
        return None, "토큰 형식이 아니에요. ghp_로 시작하는 토큰을 공백 없이 붙여 주세요."
    if not await deps.github_token_ok(token):
        return None, "이 토큰으로 GitHub에 로그인할 수 없어요. 토큰을 다시 확인해 주세요."
    if not await deps.github_repo_ok(repo, token):
        return None, "이 토큰으로 저장소를 열 수 없어요. 주소와 토큰 권한(repo)을 확인해 주세요."
    return {"github_repo": repo, "github_token": token}, None


# ── 흐름 ───────────────────────────────────────────────────

async def handle(turn: Any, team: dict, value: dict | None, deps: Deps) -> bool:
    """명단 팀원의 턴. 처리했으면 True. 연결된 팀이고 연결 버튼이 아니면 False(평소 흐름으로)."""
    turn.setup_deps = deps
    action = (value or {}).get("action")
    config = deps.get_config(team["scope"])
    if action not in SETUP_ACTIONS:
        if config is not None:
            return False
        await intro(turn, team)
        return True
    store, scope = deps.store(), team["scope"]
    if action == "setup_help":
        await turn.send(_GUIDES.get(str(value.get("kind")), _GUIDES["notion"]))
        return True
    if action == "setup_start":
        result = await asyncio.to_thread(teams.begin_setup, store, scope, turn.aad_id)
        log.info("team setup begin scope=%s result=%s", scope, result)
        if result == "busy":
            await intro(turn, team)
        elif result in ("connected", "unknown"):
            await turn.send("팀 연결은 이미 끝났어요. 메뉴에서 시작해 주세요.")
        else:
            await _notion_card(turn)
        return True
    if action == "setup_notion":
        current = await asyncio.to_thread(teams.get, store, scope) or {}
        if current.get("status") != "setting_up" or current.get("setup_by") != turn.aad_id:
            await turn.replace("팀 노션 페이지", "입력값은 지웠어요.")
            await intro(turn, team) if config is None else await turn.send("팀 연결은 이미 끝났어요.")
            return True
        clean, error = await _check("notion", value or {}, deps)
        if clean is None:
            await turn.replace("팀 노션 페이지", "확인하지 못했어요. 입력값은 지웠어요.")
            await _notion_card(turn, error)
            return True
        await turn.replace("팀 노션 페이지", "✓ 노션 연결을 확인했어요.")
        values = {"notion_token": clean["notion_token"], "notion_page_id": clean["notion_page_id"],
                  "team_name": team["name"], "admin_aad_id": turn.aad_id}
        result = await asyncio.to_thread(teams.finish_setup, store, scope, turn.aad_id,
                                         lambda: deps.save_config(scope, values), ("notion_token", "notion_page_id"))
        log.info("team setup finish scope=%s result=%s", scope, result)
        if result == "connected":
            deps.after_connect(scope, clean["notion_page_id"], clean["notion_token"])
            await _info(turn, "2 / 2 · GitHub (선택)", "GitHub도 연결할까요?\n이슈와 개발 진행 상황을 같이 볼 수 있어요. 저장소가 아직 없으면 나중에 해도 돼요.",
                        [{"title": "연결하기", "data": {"action": "setup_github_open"}},
                         {"title": "나중에 할게요", "data": {"action": "setup_github_later"}}])
        elif result == "already":
            await turn.send("다른 팀원이 먼저 팀 연결을 끝냈어요. 이제 메뉴에서 바로 쓸 수 있어요.")
        else:
            await turn.send("다른 팀원이 이어서 연결하고 있어요. 끝나면 바로 쓸 수 있어요.")
        return True
    if config is None:  # 여기부터는 연결된 팀에서만(GitHub·보드 단계)
        await intro(turn, team)
        return True
    if action == "setup_github_open":
        if config.get("github_repo"):
            await _done_card(turn, team, config)
            return True
        await _github_card(turn)
        return True
    if action == "setup_github_later" or action == "setup_board_skip":
        await _done_card(turn, team, deps.get_config(scope))
        return True
    if action in ("setup_github", "setup_board_create"):
        # 같은 팀 두 사람이 동시에 눌러도 저장소 연결·보드 만들기는 한 번만 한다(설정을 잠금 안에서 다시 읽는다)
        async with _scope_locks.setdefault(scope, asyncio.Lock()):
            return await _github_steps(turn, team, value, deps, action, deps.get_config(scope) or {})
    return False


async def _github_steps(turn: Any, team: dict, value: dict | None, deps: Deps, action: str, config: dict) -> bool:
    """GitHub 연결·보드 단계(잠금 안에서 부른다)."""
    store, scope = deps.store(), team["scope"]
    if action == "setup_github":
        if config.get("github_repo"):  # 이미 연결된 값은 덮어쓰지 않는다(바꾸려면 팀 설정에서)
            await turn.replace("팀 저장소", "입력값은 지웠어요. GitHub는 이미 연결돼 있어요.")
            await _done_card(turn, team, config)
            return True
        clean, error = await _check("github", value or {}, deps)
        if clean is None:
            await turn.replace("팀 저장소", "확인하지 못했어요. 입력값은 지웠어요.")
            await _github_card(turn, error)
            return True
        await turn.replace("팀 저장소", "✓ GitHub 연결을 확인했어요.")
        if deps.save_config(scope, clean):
            await asyncio.to_thread(teams.record_changes, store, scope, ("github_repo", "github_token"), turn.aad_id)
        board = await deps.find_board(clean["github_repo"], clean["github_token"])
        if board:
            if deps.save_config(scope, {"github_project_id": board["id"], "github_project_number": board["number"]}):
                await asyncio.to_thread(teams.record_changes, store, scope, ("github_project_number",), turn.aad_id)
            await _done_card(turn, team, deps.get_config(scope))
            return True
        await _info(turn, "작업 보드가 없네요", "만들어 드릴까요?\n할 일 · 진행 중 · 완료 칸이 있는 보드를 저장소에 붙여 드려요.",
                    [{"title": "만들어 주세요", "data": {"action": "setup_board_create"}},
                     {"title": "괜찮아요", "data": {"action": "setup_board_skip"}}])
        return True
    if action == "setup_board_create":
        if not config.get("github_repo") or config.get("github_project_number"):
            await _done_card(turn, team, config)
            return True
        board = await deps.create_board(config["github_repo"], config["github_token"], f"{team['name']} 작업 보드")
        if board and deps.save_config(scope, {"github_project_id": board["id"], "github_project_number": board["number"]}):
            await asyncio.to_thread(teams.record_changes, store, scope, ("github_project_number",), turn.aad_id)
        elif board is None:
            await turn.send("보드를 만들지 못했어요. 토큰에 project 권한이 있는지 확인해 주세요. 나중에 다시 할 수 있어요.")
        await _done_card(turn, team, deps.get_config(scope))
        return True
    return False
