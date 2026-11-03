"""팀봇 서버 — 에코봇(Teams 입력단)이 넘겨준 대화 한 턴을 처리한다.

에코봇은 Teams에서 받은 메시지를 아래 JSON으로 묶어 `POST /api/turn`으로 보내고,
서버는 처리 과정을 NDJSON(한 줄에 JSON 하나) 이벤트로 흘려보낸다.

요청 (TurnRequest)
    {"kind": "message" | "welcome",
     "user": {"aad_id": "...", "name": "..."},
     "team": {"id": "...", "name": "..."} | null,      # 1:1 채팅이면 null
     "text": "2",                                       # 사용자가 친 텍스트
     "value": {"action": "menu", "choice": "2"} | null} # 카드 버튼 data

응답 이벤트 (한 줄씩)
    {"type": "status",  "text": "🔍 확인하는 중..."}   → 에코봇: ctx.stream.update
    {"type": "chunk",   "text": "요약 조각"}           → 에코봇: ctx.stream.emit
    {"type": "message", "text": "최종 답"}             → 스트림이 열려 있으면 emit+close, 아니면 ctx.send
    {"type": "buttons", "title": "...", "emphasis": bool,
     "buttons": [{"title": "...", "data": {...}}]}       → 에코봇: 버튼 카드로 만들어 전송
    {"type": "done"}                                     → 열린 스트림 닫기
    {"type": "ping"}                                     → 아직 작업 중 (화면 표시 없음, KEEPALIVE_SECONDS마다)

한 요청은 TURN_DEADLINE_SECONDS(100초) 안에 끝난다. 넘으면 안내 메시지를 보내고 그만둔다.

Teams SDK에 전혀 의존하지 않는다. 카드 모양도 "제목 + 버튼 목록"이라는 의미만 보내고,
실제 Adaptive Card는 에코봇이 만든다.
"""

import asyncio
from datetime import datetime, timedelta, timezone, tzinfo
import concurrent.futures
import hashlib
import json
import logging
import os
import re
import secrets
import threading
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import StreamingResponse
from notion_client import Client
from pydantic import BaseModel

# .env는 아래 모듈들보다 먼저 읽는다. worklog_bridge·notion_read는 불러오는 순간 환경변수(GROQ_API_KEY,
# NOTION_RATE 등)를 읽어 두기 때문에, 순서가 바뀌면 .env 값이 그쪽에 전달되지 않는다.
load_dotenv(os.environ.get("ENV_FILE"))

import notion_map  # noqa: E402
import worklog_bridge  # noqa: E402

# ── 설정 ────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO)
BOT_SERVER_SECRET = os.environ["BOT_SERVER_SECRET"]  # 에코봇↔서버 간 검증용 (양쪽 .env에 같은 값)
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")
SERVER_PORT = int(os.environ.get("SERVER_PORT", "8000"))
DISPLAY_TZ = os.environ.get("DISPLAY_TZ", "Asia/Seoul")  # 사용자에게 보여주는 시각의 시간대
# Groq 한 번에 보내는 원문 상한(추정 토큰). 무료 등급은 분당 8,000토큰이라, 이보다 크면 413(요청이 너무 큼)으로 거절된다.
GROQ_INPUT_TOKENS = int(os.environ.get("GROQ_INPUT_TOKENS", "4000"))
# 위 토큰 예산을 한국어 글자 수로 바꾼 값(한글 1자 = UTF-8 3바이트, 4바이트 ≈ 1토큰으로 어림)
_GROQ_INPUT_CHARS = GROQ_INPUT_TOKENS * 4 // 3
# 한 요청의 최대 처리 시간과, 그동안 에코봇에 "아직 작업 중" 신호를 보내는 간격.
TURN_DEADLINE_SECONDS = float(os.environ.get("TURN_DEADLINE_SECONDS", "100"))
KEEPALIVE_SECONDS = float(os.environ.get("KEEPALIVE_SECONDS", "15"))
_TURN_TIMEOUT_TEXT = "⏳ Notion·GitHub 응답이 늦어 이번 요청을 마치지 못했어요.\n잠시 후 다시 시도해 줄래요?"

_ISSUE_REF_RE = re.compile(r"#(\d+)")

# create_task로 띄운 작업은 참조를 안 잡아두면 끝나기 전에 GC될 수 있어서 여기 모아둔다.
_background_tasks: set[asyncio.Task] = set()


def _spawn(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


# ── 캐시 (메모리, 서버 프로세스 살아있는 동안만 유지) ──────────
_project_status_field_cache: dict[tuple[str, int], tuple[str, str, str] | None] = {}
_project_item_id_cache: dict[tuple[str, int], str | None] = {}
_meeting_notes_classification_cache: dict[str, dict] = {}
# team_key -> {"hash": 요약한 원문의 해시, "summary": 요약} — 저장된 팀 정보 원문이 같으면 Groq를 다시 부르지 않는다.
_project_summary_cache: dict[str, dict] = {}

app = FastAPI()


# ── 대화 한 턴 (에코봇 요청 1건) ────────────────────────────
class TurnUser(BaseModel):
    aad_id: str
    name: str | None = None


class TurnTeam(BaseModel):
    id: str
    name: str | None = None


class TurnConversation(BaseModel):
    id: str
    type: str | None = None  # "personal" | "channel" | "groupChat"


class TurnRequest(BaseModel):
    kind: str = "message"  # "message" | "welcome"
    user: TurnUser
    team: TurnTeam | None = None
    text: str = ""
    value: dict | None = None
    # 원문 기록용 (예전 에코봇은 안 보낸다 — 없으면 기록 키를 사용자 기준으로 만든다)
    conversation: TurnConversation | None = None
    message_id: str | None = None
    sent_at: str | None = None


class Turn:
    """예전 코드의 ctx(ActivityContext) 자리를 대신한다.

    ctx.send → turn.send, ctx.stream.update → turn.status, ctx.stream.emit(조각) → turn.chunk.
    실제로 Teams에 보내는 건 에코봇이고, 여기서는 이벤트를 큐에 넣기만 한다.
    """

    def __init__(self, req: TurnRequest):
        self.req = req
        self.aad_id = req.user.aad_id
        self.user_name = req.user.name
        # 팀 채널이면 팀 단위, 1:1 채팅이면 사람 단위 — 예전 _get_team_key와 같은 규칙.
        self.team_key = f"team:{req.team.id}" if req.team else f"user:{req.user.aad_id}"
        self.team_name = req.team.name if req.team else None
        self.queue: asyncio.Queue[dict | None] = asyncio.Queue()

    def _put(self, event: dict) -> None:
        self.queue.put_nowait(event)

    def status(self, text: str) -> None:
        self._put({"type": "status", "text": text})

    def chunk(self, text: str) -> None:
        self._put({"type": "chunk", "text": text})

    def done(self) -> None:
        self._put({"type": "done"})

    async def send(self, text: str) -> None:
        self._put({"type": "message", "text": text})

    async def send_buttons(self, title: str, buttons: list[dict], emphasis: bool = False) -> None:
        self._put({"type": "buttons", "title": title, "emphasis": emphasis, "buttons": buttons})

    def finish(self) -> None:
        self.queue.put_nowait(None)


@app.post("/api/turn")
async def turn_endpoint(req: TurnRequest, x_bot_secret: str | None = Header(None)):
    if not x_bot_secret or not secrets.compare_digest(x_bot_secret, BOT_SERVER_SECRET):
        raise HTTPException(status_code=401, detail="invalid secret")

    turn = Turn(req)

    async def run() -> None:
        try:
            # 외부 API가 아무리 느려도 한 요청은 이 시간 안에 끝낸다. 넘으면 사용자에게 알리고 그만둔다.
            await asyncio.wait_for(handle_turn(turn), timeout=TURN_DEADLINE_SECONDS)
        except asyncio.TimeoutError:
            await turn.send(_TURN_TIMEOUT_TEXT)
        except Exception as exc:
            traceback.print_exc()
            await turn.send(f"⚠️ 처리 중 오류가 발생했어요: {exc}")
        finally:
            turn.finish()

    # 처리는 별도 작업으로 돌린다. 에코봇 연결이 중간에 끊겨도 설정 저장 같은 일은 끝까지 간다.
    _spawn(run())

    async def events():
        while True:
            try:
                event = await asyncio.wait_for(turn.queue.get(), timeout=KEEPALIVE_SECONDS)
            except asyncio.TimeoutError:
                # 아직 작업 중이라는 신호. 에코봇은 이걸로 "끊김"과 "느림"을 구분한다(화면엔 안 보임).
                yield '{"type": "ping"}\n'
                continue
            if event is None:
                break
            yield json.dumps(event, ensure_ascii=False) + "\n"

    return StreamingResponse(events(), media_type="application/x-ndjson")


@app.get("/health")
async def health():
    return {"status": "ok"}


# ── 팀/사용자별 설정 저장소 ────────────────────────────────────────
TEAM_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "team_configs.json")
MEMBER_PROFILE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "member_profiles.json")


def _load_team_configs() -> dict:
    try:
        with open(TEAM_CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_team_configs(data: dict) -> None:
    with open(TEAM_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _load_member_profiles() -> dict:
    try:
        with open(MEMBER_PROFILE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_member_profiles(data: dict) -> None:
    with open(MEMBER_PROFILE_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _get_github_id(aad_id: str) -> str | None:
    return _load_member_profiles().get(aad_id, {}).get("github_id")


def _set_github_id(aad_id: str, github_id: str) -> None:
    profiles = _load_member_profiles()
    profiles.setdefault(aad_id, {})["github_id"] = github_id
    _save_member_profiles(profiles)


def _get_config(team_key: str) -> dict | None:
    """team_key로 저장된 설정을 반환한다. 없으면 None — 다른 사람/팀 설정으로 조용히 넘어가지 않는다."""
    return _load_team_configs().get(team_key)


def _notion_client_for(config: dict, background: bool = False):
    """Notion 클라이언트는 항상 토큰별 관문을 지나는 것으로 만든다(notion_read.notion_client_for 참고)."""
    return notion_client_for(config["notion_token"], background=background)


# ── 설정 마법사 입력값 파싱 ──────────────────────────────
def _extract_github_repo(text: str) -> str | None:
    text = text.strip()
    m = re.search(r"github\.com/([^/\s]+/[^/\s]+)", text)
    if m:
        return m.group(1).removesuffix(".git").rstrip("/")
    if re.fullmatch(r"[\w.-]+/[\w.-]+", text):
        return text
    return None


def _extract_notion_page_id(text: str) -> str | None:
    stripped = text.strip().replace("-", "")
    m = re.search(r"([0-9a-fA-F]{32})", stripped)
    return m.group(1) if m else None


def _extract_project_number(text: str) -> int | None:
    text = text.strip()
    return int(text) if text.isdigit() else None


def _extract_token(text: str) -> str | None:
    text = text.strip()
    # 공백 없이 + ASCII만. non-ASCII가 섞이면 httpx가 헤더에 넣다가 UnicodeEncodeError로 죽는다.
    if not text or re.search(r"\s", text) or not text.isascii():
        return None
    return text


_GITHUB_USERNAME_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}$")


def _extract_github_id(text: str) -> str | None:
    text = text.strip()
    return text if _GITHUB_USERNAME_RE.fullmatch(text) else None


async def _github_token_valid(token: str) -> bool:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://api.github.com/user",
                headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
            )
        return resp.status_code == 200
    except Exception:
        return False


def _notion_page_accessible(page_id: str, notion_client: Client) -> bool:
    """페이지 'Connections'에 이 integration이 추가돼 있어야만 API로 읽을 수 있다."""
    try:
        notion_client.pages.retrieve(page_id)
        return True
    except Exception:
        pass
    try:
        notion_client.databases.retrieve(page_id)
        return True
    except Exception:
        return False


# ── 메뉴 / 문구 ────────────────────────────────────────
WELCOME_TEXT = "안녕하세요! 아래에서 골라주세요\n\n⚙️ 연결 설정을 바꾸고 싶으면 '설정'이라고 입력하세요."

_MENU_ITEMS = [
    ("1", "1️⃣ 팀/프로젝트 정보"),
    ("2", "2️⃣ 진행상황 조회"),
    ("3", "3️⃣ 내 역할 조회"),
    ("4", "4️⃣ 완료된 작업 공유하기"),
]
_MENU_BUTTONS = [{"title": label, "data": {"action": "menu", "choice": choice}} for choice, label in _MENU_ITEMS]

# 예전엔 on_message_pattern("hello|hi|greetings")가 부분 일치라 "github" 같은 입력에도 걸릴 수 있었다.
# 정확히 그 단어만 보냈을 때만 인사한다.
_GREETING_RE = re.compile(r"(hello|hi|greetings)", re.IGNORECASE)


# ── 대화 상태 (서버 메모리) ────────────────────────────────
# key: aad_object_id, value: {"team_key": str, "step": int, "data": dict}
setup_sessions: dict[str, dict] = {}
# key: aad_object_id, value: {"kind": "menu", "raw_text": str} 또는 {"kind": "claim", "issue_number": int}
github_onboarding_sessions: dict[str, dict] = {}


@dataclass
class SetupStep:
    key: str
    prompt: str
    parser: Callable[[str], object | None]
    error_message: str


SETUP_STEPS: list[SetupStep] = [
    SetupStep("github_repo", "**1** 레포\n`thdwlndjs/n8nTest`", _extract_github_repo,
              "레포 형식이 아니에요.\n`owner/repo`로 다시 보내줄래요?"),
    SetupStep("github_token",
              "**2** GitHub 토큰\n(repo, project 권한 있는 Personal Access Token)\n\n모르면 '가이드'라고 보내주세요",
              _extract_token, "토큰 형식이 아니에요. 공백 없이 다시 보내줄래요?"),
    SetupStep("admin_github_id", "**3** 본인 GitHub 아이디\n`thdwlndjs`처럼 아이디만 보내주세요",
              _extract_github_id, "GitHub 아이디 형식이 아니에요. (예: thdwlndjs)"),
    SetupStep("notion_token", "**4** Notion 토큰\n(Integration Token)\n\n모르면 '가이드'라고 보내주세요",
              _extract_token, "토큰 형식이 아니에요. 공백 없이 다시 보내줄래요?"),
    SetupStep("notion_page_id", "**5** 팀 정보 노션 페이지 링크", _extract_notion_page_id,
              "페이지를 못 찾았어요.\n링크를 다시 보내줄래요?"),
    SetupStep("meeting_notes_page_id", "**6** 회의록 노션 페이지 링크", _extract_notion_page_id,
              "페이지를 못 찾았어요.\n링크를 다시 보내줄래요?"),
    SetupStep("github_project_number", "**7** GitHub 프로젝트 번호\nProjects 탭 URL 끝 숫자예요",
              _extract_project_number, "숫자만 보내줄래요?"),
]

_HELP_TRIGGERS = {"모름", "몰라", "모르겠어", "가이드", "도움말", "help"}
_YES_WORDS = {"네", "예", "응", "어", "맞아", "맞아요", "yes", "y"}
_NO_WORDS = {"아니", "아니요", "아니오", "no", "n"}
_CANCEL_TRIGGERS = {"나가기", "취소", "x", "9"}

_ASK_GITHUB_ID_TEXT = (
    "먼저 GitHub 아이디를 알려줄래요?\n`thdwlndjs`처럼 아이디만 보내주세요.\n\n"
    "(그만두려면 '취소' 또는 '나가기')"
)
_NO_CONFIG_TEXT = "아직 연결된 설정이 없어요.\n'설정'이라고 입력해서 먼저 연결해줄래요?"
_SETUP_GUIDES: dict[str, str] = {
    "github_token": (
        "**GitHub 토큰 발급 가이드**\n\n"
        "1. https://github.com/settings/tokens 접속\n"
        "(GitHub 우측 상단 프로필 → Settings → 왼쪽 메뉴 맨 아래 Developer settings → Personal access tokens)\n\n"
        "2. **Generate new token (classic)** 선택 — Fine-grained 말고 꼭 Classic\n\n"
        "3. Note에 이름 아무거나(예: teamsAgent), 만료일 설정\n\n"
        "4. Select scopes에서 최상위 체크박스 딱 2개만 체크: **`repo`**, **`project`**\n\n"
        "5. 맨 아래 Generate token → 나오는 `ghp_...` 토큰을 복사해서 여기에 보내주세요\n\n"
        "토큰 다시 보내줄래요?"
    ),
    "notion_token": (
        "**Notion 토큰 발급 가이드**\n\n"
        "1. https://app.notion.com/developers/connections 접속\n\n"
        "2. **신규 연결** 선택\n\n"
        "3. 이름 아무거나 입력하고, 아래 항목 체크\n"
        "- 콘텐츠 기능: 읽기, 업데이트, 삽입 (셋 다 체크)\n"
        "- 이메일 주소를 포함한 사용자 정보 읽기\n\n"
        "4. 만들어진 **액세스 토큰** 복사\n\n"
        "5. 연결하려는 Notion 페이지로 가서 우측 상단 `···` → **Connections** → 방금 만든 integration 추가\n"
        "(이 연결을 안 해두면 페이지 검증 단계에서 계속 실패해요)\n\n"
        "토큰 다시 보내줄래요?"
    ),
}
_SECRET_KEYS = {"github_token", "notion_token"}
_SETUP_LABELS = {
    "github_repo": "레포",
    "github_token": "GitHub 토큰",
    "admin_github_id": "내 GitHub 아이디",
    "notion_token": "Notion 토큰",
    "notion_page_id": "팀 정보",
    "meeting_notes_page_id": "회의록",
    "github_project_number": "프로젝트 번호",
}


# ── 턴 라우팅 (예전 handle_message의 판단 부분) ─────────────────
async def handle_turn(turn: Turn) -> None:
    if turn.req.kind == "welcome":
        await turn.send_buttons(WELCOME_TEXT, _MENU_BUTTONS)
        return

    # 카드 버튼(Action.Submit / Action.Execute) — 에코봇이 data를 value에 그대로 실어 보낸다.
    value = turn.req.value
    if isinstance(value, dict):
        action = value.get("action")
        if action == "menu" and "choice" in value:
            await _run_action(turn, str(value["choice"]))
            return
        if action == "claim_issue" and "issue_number" in value:
            await _handle_claim_click(turn, int(value["issue_number"]))
            return
        if action == "dismiss_claim_card":
            await turn.send("알겠어요, 넘어갈게요.")
            return

    raw_text = turn.req.text.strip()
    aad_id = turn.aad_id

    if _is_team_record(aad_id, raw_text):
        await asyncio.to_thread(_record_turn_text, turn, raw_text)

    if raw_text.lower() in _CANCEL_TRIGGERS and _cancel_pending_sessions(aad_id):
        await turn.send("취소했어요. 처음부터 다시 하려면 원하는 메뉴나 '설정'을 입력해주세요.")
        return

    if aad_id in setup_sessions:
        await _handle_setup_message(turn, raw_text)
        return

    if aad_id in github_onboarding_sessions:
        await _handle_github_onboarding_message(turn, raw_text)
        return

    if raw_text in ("설정", "0"):
        await _start_setup(turn)
        return

    if _GREETING_RE.fullmatch(raw_text):
        await turn.send("Hello! How can I assist you today?")
        return

    if not re.fullmatch(r"[1-4]", raw_text):
        await turn.send("올바른 값을 입력하세요. (1~4, 연결 설정 변경은 '설정')")
        return

    await _run_action(turn, raw_text)


def _is_team_record(aad_id: str, raw_text: str) -> bool:
    """팀 기록으로 남길 메시지인지. 메뉴 명령·인사, 그리고 토큰이 오갈 수 있는 설정/온보딩 답은 남기지 않는다."""
    if not raw_text or aad_id in setup_sessions or aad_id in github_onboarding_sessions:
        return False
    lowered = raw_text.lower()
    return not (
        lowered in _CANCEL_TRIGGERS
        or raw_text in ("설정", "0")
        or re.fullmatch(r"[1-4]", raw_text)
        or _GREETING_RE.fullmatch(raw_text)
    )


def _record_turn_text(turn: Turn, raw_text: str) -> None:
    req = turn.req
    conversation_id = req.conversation.id if req.conversation else turn.team_key
    message_ref = req.message_id or db_safe_hash(f"{turn.aad_id}|{req.sent_at}|{raw_text}")
    worklog_bridge.record(
        scope=turn.team_key,
        kind="teams_message",
        conversation_key=f"teams:{conversation_id}",
        locator=f"teams:{conversation_id}:{message_ref}",
        text=raw_text,
        author=turn.user_name or turn.aad_id,
        sent_at=req.sent_at,
    )


def db_safe_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]


def _cancel_pending_sessions(aad_id: str) -> bool:
    cancelled = False
    if aad_id in setup_sessions:
        del setup_sessions[aad_id]
        cancelled = True
    if aad_id in github_onboarding_sessions:
        del github_onboarding_sessions[aad_id]
        cancelled = True
    return cancelled


# ── 초기 설정 마법사 ────────────────────────────────────────
async def _start_setup(turn: Turn) -> None:
    existing = _get_config(turn.team_key)
    if existing is not None and existing.get("admin_aad_id") != turn.aad_id:
        await turn.send("이미 설정돼 있어요.\n이 설정은 처음 등록한 관리자만 바꿀 수 있어요.")
        return

    setup_sessions[turn.aad_id] = {"team_key": turn.team_key, "step": 0, "data": {}}
    header = f"**{turn.team_name}** 설정 시작할게요" if turn.team_name else "설정 시작할게요"
    await turn.send(f"{header}\n\n{SETUP_STEPS[0].prompt}\n\n(중간에 그만두려면 '취소' 또는 '나가기')")


async def _handle_setup_message(turn: Turn, raw_text: str) -> None:
    aad_id = turn.aad_id
    session = setup_sessions[aad_id]
    step_index = session["step"]
    step = SETUP_STEPS[step_index]
    key = step.key

    if raw_text.strip().lower() in _HELP_TRIGGERS and key in _SETUP_GUIDES:
        await turn.send(_SETUP_GUIDES[key])
        return

    value = step.parser(raw_text)
    if value is None:
        await turn.send(f"⚠️ {step.error_message}")
        return

    if key == "github_token" and not await _github_token_valid(value):
        await turn.send("⚠️ 이 토큰으로 GitHub 인증이 안 돼요.\n토큰이 맞는지 다시 확인해서 보내줄래요?")
        return

    if key == "notion_token" and value == session["data"].get("github_token"):
        await turn.send(
            "⚠️ 방금 보낸 값이 GitHub 토큰이랑 똑같아요.\n"
            "Notion 토큰 대신 GitHub 토큰을 잘못 다시 보낸 것 같은데, Notion 토큰을 다시 확인해서 보내줄래요?"
        )
        return

    if key in ("notion_page_id", "meeting_notes_page_id"):
        notion_client = notion_client_for(session["data"]["notion_token"])
        accessible = await asyncio.to_thread(_notion_page_accessible, value, notion_client)
        if not accessible:
            await turn.send(
                "⚠️ 이 페이지는 방금 넣은 Notion 토큰으로 불러올 수 없어요.\n"
                "게스트 권한만 있거나, 이 토큰이 아직 그 페이지의 연결(Connections)에 "
                "추가 안 됐을 수 있어요.\n"
                "페이지 주인에게 연결 추가를 부탁하거나, 다른 페이지 링크를 보내줄래요?"
            )
            return
        if key == "notion_page_id":
            # 팀 정보 페이지를 등록한 순간 구조·원문을 읽어 저장해 둔다 → 1번은 이걸로 바로 답한다.
            _spawn(_register_team_map(session["team_key"], [value], session["data"]["notion_token"]))
        else:
            # 회의록 페이지도 등록한 순간 읽어 둔다(팀 정보 안에 있으면 다시 읽지 않는다) → 2·3번이 바로 답한다.
            _spawn(_register_team_map(session["team_key"], [session["data"]["notion_page_id"], value],
                                      session["data"]["notion_token"]))

    session["data"][key] = value
    step_index += 1

    if step_index >= len(SETUP_STEPS):
        summary = "\n".join(
            f"- {_SETUP_LABELS[k]}: {'등록됨' if k in _SECRET_KEYS else v}"
            for k, v in session["data"].items()
            if k in _SETUP_LABELS
        )
        admin_github_id = session["data"].pop("admin_github_id")
        _set_github_id(aad_id, admin_github_id)

        session["data"]["admin_aad_id"] = aad_id
        configs = _load_team_configs()
        configs[session["team_key"]] = session["data"]
        _save_team_configs(configs)
        del setup_sessions[aad_id]
        await turn.send(f"**✅ 설정 완료**\n\n{summary}\n\n이제 1~4번 이 설정으로 동작해요.")
        return

    session["step"] = step_index
    await turn.send(f"✅ {_SETUP_LABELS[key]} 완료!\n\n{SETUP_STEPS[step_index].prompt}")


# ── 메뉴 실행 ────────────────────────────────────────
async def _run_action(turn: Turn, raw_text: str) -> None:
    config = _get_config(turn.team_key)
    if config is None:
        await turn.send(_NO_CONFIG_TEXT)
        return

    github_id = _get_github_id(turn.aad_id)
    if raw_text in ("3", "4") and github_id is None:
        github_onboarding_sessions[turn.aad_id] = {"kind": "menu", "raw_text": raw_text}
        await turn.send(_ASK_GITHUB_ID_TEXT)
        return

    try:
        if raw_text == "1":
            # 요약 글자가 chunk 이벤트로 흘러가고, done으로 스트림을 닫는다.
            await fetch_project_info(config, turn.team_key, on_stage=turn.status, on_chunk=turn.chunk)
            turn.done()
            return
        elif raw_text == "2":
            turn.status("🔍 진행상황을 확인하는 중...")
            reply_text, unassigned_issues = await fetch_team_status(config, turn.team_key, on_stage=turn.status)
            await turn.send(reply_text)
            if unassigned_issues:
                await turn.send_buttons(*_claim_buttons(unassigned_issues), emphasis=True)
            return
        elif raw_text == "3":
            turn.status("🔍 내 역할을 확인하는 중...")
            reply_text = await fetch_my_role(github_id, turn.user_name, config, turn.team_key, on_stage=turn.status)
        elif raw_text == "4":
            turn.status("🔍 완료된 작업을 확인하는 중...")
            reply_text = await share_completed_work(config, github_id)
        else:
            reply_text = "올바른 값을 입력하세요. (1~4, 연결 설정 변경은 '설정')"
    except Exception as exc:
        traceback.print_exc()
        reply_text = f"⚠️ 처리 중 오류가 발생했어요: {exc}"

    # status를 띄운 뒤의 message는 에코봇이 같은 스트림에 emit+close해서 "확인하는 중..."을 교체한다.
    await turn.send(reply_text)


def _claim_buttons(issues: list[dict]) -> tuple[str, list[dict]]:
    title = "🙋 담당자 없는 이슈 — 맡고 싶은 걸 눌러주세요"
    buttons = [
        {"title": f"#{i['number']} {i['title']}"[:40], "data": {"action": "claim_issue", "issue_number": i["number"]}}
        for i in issues
    ]
    buttons.append({"title": "🚪 나가기", "data": {"action": "dismiss_claim_card"}})
    return title, buttons


async def _handle_claim_click(turn: Turn, issue_number: int) -> None:
    if _get_config(turn.team_key) is None:
        await turn.send(_NO_CONFIG_TEXT)
        return

    github_id = _get_github_id(turn.aad_id)
    if github_id is None:
        github_onboarding_sessions[turn.aad_id] = {"kind": "claim", "issue_number": issue_number}
        await turn.send(_ASK_GITHUB_ID_TEXT)
        return

    await _claim_and_reply(turn, issue_number, github_id)


async def _claim_and_reply(turn: Turn, issue_number: int, github_id: str) -> None:
    config = _get_config(turn.team_key)
    if config is None:
        await turn.send(_NO_CONFIG_TEXT)
        return
    try:
        reply_text = await claim_issue(config, issue_number, github_id)
    except Exception as exc:
        traceback.print_exc()
        reply_text = f"⚠️ 처리 중 오류가 발생했어요: {exc}"
    await turn.send(reply_text)


async def _handle_github_onboarding_message(turn: Turn, raw_text: str) -> None:
    aad_id = turn.aad_id
    session = github_onboarding_sessions[aad_id]

    if "pending_confirm" in session:
        answer = raw_text.strip().lower()
        if answer in _YES_WORDS:
            github_id = session.pop("pending_confirm")
        elif answer in _NO_WORDS:
            del session["pending_confirm"]
            await turn.send("그럼 정확한 GitHub 아이디를 다시 보내줄래요?")
            return
        else:
            await turn.send("⚠️ '네' 또는 '아니요'로 답해줄래요?")
            return
    else:
        github_id = _extract_github_id(raw_text)
        if github_id is None:
            await turn.send("⚠️ GitHub 아이디 형식이 아니에요. (예: thdwlndjs)")
            return
        if len(github_id) == 1 and github_id.isdigit():
            session["pending_confirm"] = github_id
            await turn.send(f"GitHub 아이디가 흔치 않은 형태인데, 정말 `{github_id}`가 맞으세요? (네/아니요)")
            return

    del github_onboarding_sessions[aad_id]
    _set_github_id(aad_id, github_id)
    await turn.send(f"연결 완료! GitHub 아이디: `{github_id}`")

    if session["kind"] == "claim":
        await _claim_and_reply(turn, session["issue_number"], github_id)
    else:
        await _run_action(turn, session["raw_text"])


# ── GitHub API 헬퍼 ────────────────────────────────────
_github_http_client: httpx.AsyncClient | None = None


def _get_github_http_client() -> httpx.AsyncClient:
    global _github_http_client
    if _github_http_client is None:
        _github_http_client = httpx.AsyncClient(timeout=15)
    return _github_http_client


@app.on_event("shutdown")
async def _close_github_http_client() -> None:
    global _github_http_client
    if _github_http_client is not None:
        await _github_http_client.aclose()
        _github_http_client = None


async def _github_get(repo: str, path: str, token: str, params: dict | None = None) -> dict | list[dict]:
    url = f"https://api.github.com/repos/{repo}{path}"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    resp = await _get_github_http_client().get(url, headers=headers, params=params)
    resp.raise_for_status()
    return resp.json()


# (token 지문, repo, path, params) -> {"data", "expires_at"}
# 같은 레포를 권한이 다른 토큰 두 개가 쓸 때 결과가 섞이지 않게 토큰 지문도 키에 넣는다.
_github_get_cache: dict[tuple, dict] = {}
_GITHUB_GET_CACHE_TTL_SECONDS = 30


async def _github_get_cached(
    repo: str, path: str, token: str, params: dict | None = None, ttl: float = _GITHUB_GET_CACHE_TTL_SECONDS
) -> dict | list[dict]:
    token_fp = hashlib.sha256(token.encode()).hexdigest()[:16]
    key = (token_fp, repo, path, tuple(sorted((params or {}).items())))
    now = time.monotonic()
    cached = _github_get_cache.get(key)
    if cached and cached["expires_at"] > now:
        return cached["data"]
    data = await _github_get(repo, path, token, params)
    _github_get_cache[key] = {"data": data, "expires_at": now + ttl}
    return data


async def _github_patch(repo: str, path: str, token: str, json_body: dict) -> dict:
    url = f"https://api.github.com/repos/{repo}{path}"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    resp = await _get_github_http_client().patch(url, headers=headers, json=json_body)
    resp.raise_for_status()
    return resp.json()


async def _github_graphql(query: str, token: str, variables: dict | None = None) -> dict:
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    resp = await _get_github_http_client().post(
        "https://api.github.com/graphql",
        headers=headers,
        json={"query": query, "variables": variables or {}},
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("errors"):
        raise RuntimeError(f"GitHub GraphQL error: {data['errors']}")
    return data["data"]


# ── 노션 페이지 읽기 (notion_read.py) ───────────────────────────────
from notion_read import (  # noqa: E402
    notion_client_for, _block_text, _fetch_notion_page_by_id, _inline_database_lines, _list_child_blocks, _map_parallel,
    _query_database_rows, _rich_text,
)


# ── 1. 팀/프로젝트 정보 (Groq 스트리밍 요약) ─────────────────────
def _fit_for_groq(text: str, keep: str = "head") -> str:
    """Groq 입력 예산(GROQ_INPUT_TOKENS)에 맞게 자른다. keep="tail"이면 뒤쪽(최근 것)을 남긴다."""
    budget = GROQ_INPUT_TOKENS * 4  # 추정: 4바이트 ≈ 1토큰
    data = text.encode("utf-8")
    if len(data) <= budget:
        return text
    if keep == "tail":
        return "(앞부분 생략)\n" + data[-budget:].decode("utf-8", errors="ignore")
    return data[:budget].decode("utf-8", errors="ignore") + "\n(이하 생략)"


async def _summarize_project_info(raw_text: str, on_chunk: Callable[[str], None] | None = None) -> str | None:
    """요약을 돌려준다. Groq 키가 없거나 실패하면 None — 호출부가 원문 앞부분을 보여주고 캐시하지 않는다."""
    if not GROQ_API_KEY or not raw_text.strip():
        return None
    raw_text = _fit_for_groq(raw_text)

    system_prompt = (
        "너는 팀 소개/프로젝트 정보 페이지 원문을 읽고 핵심만 간결하게 요약하는 도우미야.\n"
        "팀 이름, 팀원과 역할, 프로젝트명과 목적, 현재 진행 단계 위주로 정리해.\n"
        "'[프로젝트명 입력]'처럼 아직 안 채워진 항목은 '미정'으로 짧게만 표시하거나 생략해.\n"
        "팀 규칙/협업 방식 같은 formality는 한두 줄로만 축약하고, 전체 5~10줄 이내로 정리해."
    )
    try:
        full_text = ""
        async with httpx.AsyncClient(timeout=15) as client:
            async with client.stream(
                "POST",
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                json={
                    "model": GROQ_MODEL,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": raw_text},
                    ],
                    "temperature": 0.2,
                    "stream": True,
                },
            ) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    payload = line[len("data: "):].strip()
                    if payload == "[DONE]":
                        break
                    delta = json.loads(payload)["choices"][0]["delta"].get("content", "")
                    if delta:
                        full_text += delta
                        if on_chunk:
                            on_chunk(delta)
        return full_text.strip() or None
    except Exception:
        traceback.print_exc()
        return None


def _team_roots(config: dict) -> list[str]:
    """지도에 담을 루트: 팀 정보 페이지(기준)와 회의록 페이지. 회의록이 팀 정보 안에 있으면 따로 읽지 않는다."""
    return [r for r in (config.get("notion_page_id"), config.get("meeting_notes_page_id")) if r]


_team_sync_locks: dict[str, threading.Lock] = {}
_team_sync_locks_guard = threading.Lock()


def _sync_team_map(team_key: str, roots: list[str], token: str, background: bool) -> dict:
    """팀 정보·회의록 페이지의 구조·원문을 worklog 저장소에 맞춘다(바뀐 곳만 읽는다).
    같은 팀을 동시에 두 번 맞추지 않는다(등록 직후·첫 메뉴·백그라운드 수집이 겹칠 때). 뒤에 온 쪽은 앞의 결과를
    이어받아 바뀐 곳만 본다."""
    with _team_sync_locks_guard:
        lock = _team_sync_locks.setdefault(team_key, threading.Lock())
    with lock:
        return notion_map.sync_roots(worklog_bridge.WORKLOG_STORE, team_key, roots,
                                     notion_client_for(token, background=background),
                                     classify=worklog_bridge.groq_classify_sections)


def _format_as_of(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        when = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(_display_tz())
        return when.strftime("%m/%d %H:%M")
    except Exception:
        return iso


def _display_tz() -> tzinfo:
    """표시용 시간대. Windows처럼 시간대 데이터(tzdata)가 없는 곳에서도 한국 시간은 나오게 한다."""
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(DISPLAY_TZ)
    except Exception:
        return timezone(timedelta(hours=9), "KST") if DISPLAY_TZ == "Asia/Seoul" else timezone.utc


async def _summarize_snapshot(team_key: str, snapshot: dict, on_chunk: Callable[[str], None] | None = None) -> str:
    """저장된 팀 정보 원문을 요약한다. 원문이 지난번과 같으면(해시 동일) Groq를 다시 부르지 않는다."""
    content_hash = hashlib.sha256(snapshot["text"].encode("utf-8")).hexdigest()
    cached = _project_summary_cache.get(team_key)
    if cached and cached["hash"] == content_hash:
        if on_chunk:
            on_chunk(cached["summary"])
        return cached["summary"]
    streamed = False

    def _track(delta: str) -> None:
        nonlocal streamed
        streamed = True
        if on_chunk:
            on_chunk(delta)

    text = await _summarize_project_info(snapshot["text"], on_chunk=_track)
    if text is None:
        # 요약 실패(키 없음·Groq 오류): 원문 앞부분만 보여주고, 캐시하지 않아 다음에 다시 요약을 시도한다.
        preview = snapshot["text"][:1500] + ("\n…" if len(snapshot["text"]) > 1500 else "")
        fallback = f"⚠️ 요약을 만들지 못해 저장된 원문 앞부분을 보여줘요.\n\n{preview}"
        if on_chunk and not streamed:
            on_chunk(fallback)
        return fallback
    _project_summary_cache[team_key] = {"hash": content_hash, "summary": text}
    return text


async def _register_team_map(team_key: str, roots: list[str], token: str) -> None:
    """설정에서 팀 정보·회의록 페이지를 등록한 순간, 구조와 원문을 한 번 읽어 저장하고 요약까지 만들어 둔다.
    설정 마법사는 기다리지 않는다(백그라운드). 실패해도 메뉴를 처음 누를 때 다시 시도한다."""
    try:
        await asyncio.to_thread(_sync_team_map, team_key, roots, token, False)
        snapshot = await asyncio.to_thread(worklog_bridge.team_snapshot, team_key, roots[0], _GROQ_INPUT_CHARS)
        if snapshot:
            await _summarize_snapshot(team_key, snapshot)
    except Exception:
        traceback.print_exc()


async def _first_read(config: dict, team_key: str, on_stage: Callable[[str], None] | None) -> None:
    """등록 때 읽기가 아직 안 끝났거나 실패했을 때만: 지금 한 번 읽는다(사용자 요청 우선순위)."""
    if on_stage:
        on_stage("🔍 Notion 페이지를 처음 읽는 중이에요 (한 번만 걸려요)...")
    await asyncio.to_thread(_sync_team_map, team_key, _team_roots(config), config["notion_token"], False)


async def _stored_notes(config: dict, team_key: str, on_stage: Callable[[str], None] | None = None) -> dict:
    """2·3번용 회의 기록. Notion을 다시 읽지 않고 저장본을 쓴다(처음 한 번만 읽음)."""
    notes_id = config.get("meeting_notes_page_id")
    notes = await asyncio.to_thread(worklog_bridge.meeting_notes, team_key, notes_id, _GROQ_INPUT_CHARS)
    if notes is None:
        await _first_read(config, team_key, on_stage)
        notes = await asyncio.to_thread(worklog_bridge.meeting_notes, team_key, notes_id, _GROQ_INPUT_CHARS)
    return notes or {"texts": [], "as_of": None, "omitted": 0}


def _note_footer(notes: dict) -> list[str]:
    as_of = _format_as_of(notes.get("as_of"))
    return ["", f"(Notion 기준 {as_of})"] if as_of else []


def _brief(text: str, limit: int = 120) -> str:
    """저장된 원문 한 건을 한 줄로: 머리줄([역할] 경로)을 빼고 내용 줄들을 이어 붙인다."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    body = " · ".join(lines[1:] or lines)
    return body if len(body) <= limit else body[: limit - 1] + "…"


async def fetch_project_info(
    config: dict,
    team_key: str,
    on_stage: Callable[[str], None] | None = None,
    on_chunk: Callable[[str], None] | None = None,
) -> str:
    """팀/프로젝트 정보 — 저장해 둔 구조·원문으로 답한다(Notion을 다시 읽지 않음).
    저장분은 등록할 때 처음 만들어지고, 이후 백그라운드 수집이 바뀐 곳만 갱신한다."""
    root = config["notion_page_id"]
    snapshot = await asyncio.to_thread(worklog_bridge.team_snapshot, team_key, root, _GROQ_INPUT_CHARS)
    if snapshot is None:
        await _first_read(config, team_key, on_stage)
        snapshot = await asyncio.to_thread(worklog_bridge.team_snapshot, team_key, root, _GROQ_INPUT_CHARS)
    if snapshot is None:
        text = "팀 정보 페이지에서 읽을 내용을 찾지 못했어요."
        if on_chunk:
            on_chunk(text)
        return text
    if on_stage:
        on_stage("🧠 요약 정리하는 중...")
    summary = await _summarize_snapshot(team_key, snapshot, on_chunk)
    as_of = _format_as_of(snapshot.get("as_of"))
    if as_of and on_chunk:
        on_chunk(f"\n\n(Notion 기준 {as_of})")
    return summary


# ── 회의록 → 확정 / 미정 분류 (Groq) ─────────────────────
async def _classify_meeting_notes(note_lines: list[str], cache_key: str) -> dict[str, list[str]]:
    empty = {"confirmed": [], "undecided": []}
    if not GROQ_API_KEY or not note_lines:
        return empty

    raw_text = _fit_for_groq("\n".join(note_lines), keep="tail")  # 최근 회의가 남도록 뒤쪽을 남긴다
    content_hash = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
    cached = _meeting_notes_classification_cache.get(cache_key)
    if cached and cached["hash"] == content_hash:
        return cached["result"]

    system_prompt = (
        "너는 회의록 원문을 읽고 두 갈래로 분류하는 도우미야.\n"
        "1) [확정] — 담당자와 할 일이 명확히 정해진 것. '담당자: 할 일' 형식으로 한 줄씩 적어. "
        "이슈 번호(#123) 같은 참조가 원문에 있으면 그대로 유지해.\n"
        "2) [미정] — 담당자나 방향이 아직 안 정해져서 논의만 되고 결론이 안 난 사안. 한 줄씩 적어.\n"
        "반드시 아래 형식 그대로만 출력하고, 다른 설명이나 인사말은 절대 붙이지 마:\n"
        "[확정]\n"
        "(항목들, 없으면 비워둠)\n"
        "[미정]\n"
        "(항목들, 없으면 비워둠)"
    )

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                json={
                    "model": GROQ_MODEL,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": raw_text},
                    ],
                    "temperature": 0.2,
                },
            )
            resp.raise_for_status()
            summary_text = resp.json()["choices"][0]["message"]["content"].strip()
    except Exception:
        traceback.print_exc()
        return empty

    confirmed_part, _, rest = summary_text.partition("[미정]")
    confirmed_part = confirmed_part.replace("[확정]", "")

    def _clean_lines(text: str) -> list[str]:
        lines = [line.strip("-• ").strip() for line in text.splitlines()]
        return [line for line in lines if line]

    result = {"confirmed": _clean_lines(confirmed_part), "undecided": _clean_lines(rest)}
    _meeting_notes_classification_cache[cache_key] = {"hash": content_hash, "result": result}
    return result


async def _get_commit_touched_issues(repo: str, token: str) -> set[int]:
    try:
        commits = await _github_get_cached(repo, "/commits", token, {"per_page": 30})
    except Exception:
        return set()
    touched: set[int] = set()
    for commit in commits:
        message = commit.get("commit", {}).get("message", "")
        touched |= {int(m.group(1)) for m in _ISSUE_REF_RE.finditer(message)}
    return touched


def _classify_tasks_by_github_state(
    confirmed: list[str],
    issue_state_by_number: dict[int, str],
    commit_touched: set[int],
) -> dict[str, list[str]]:
    """외부 I/O 없는 순수 함수 — 완료/진행중/대기/미등록 분류 규칙만 담는다."""
    done: list[str] = []
    in_progress: list[str] = []
    not_started: list[str] = []
    unlinked: list[str] = []

    for task in confirmed[:15]:
        referenced = [int(m.group(1)) for m in _ISSUE_REF_RE.finditer(task)]
        matched = next((n for n in referenced if n in issue_state_by_number), None)
        if matched is None:
            unlinked.append(task)
        elif issue_state_by_number[matched] == "closed":
            done.append(task)
        elif matched in commit_touched:
            in_progress.append(task)
        else:
            not_started.append(task)

    return {"done": done, "in_progress": in_progress, "not_started": not_started, "unlinked": unlinked}


# ── 팀 기록 수집·분석 (worklog) ─────────────────────────────
def _sync_notion_maps() -> dict[str, dict]:
    """설정된 모든 팀의 '팀 정보 페이지'를 루트로, 구조 지문 확인 → 바뀐 곳만 재스캔 → 지도대로 원문 수집.
    Notion은 읽기만 한다."""
    results: dict[str, dict] = {}
    for team_key, config in _load_team_configs().items():
        root = config.get("notion_page_id")
        if not root or not config.get("notion_token"):
            continue
        try:
            results[team_key] = _sync_team_map(team_key, _team_roots(config), config["notion_token"], background=True)
        except Exception:
            traceback.print_exc()
    return results


async def _worklog_loop() -> None:
    # 서버가 켜지자마자 수집을 시작하면 첫 사용자 요청과 겹친다. 잠시 기다렸다 시작한다.
    await asyncio.sleep(worklog_bridge.START_DELAY_SECONDS)
    last_collect = 0.0
    while True:
        try:
            if time.monotonic() - last_collect >= worklog_bridge.NOTION_COLLECT_SECONDS:
                last_collect = time.monotonic()
                await asyncio.to_thread(_sync_notion_maps)
            await asyncio.to_thread(worklog_bridge.analyze_due)
        except Exception:
            traceback.print_exc()
        await asyncio.sleep(worklog_bridge.ANALYZE_TICK_SECONDS)


@app.on_event("startup")
async def _start_worklog() -> None:
    if os.environ.get("WORKLOG_LOOP", "1") != "0":
        _spawn(_worklog_loop())


# ── 2. 팀 진행상황 조회 ─────────────────────────────
async def fetch_team_status(config: dict, team_key: str,
                            on_stage: Callable[[str], None] | None = None) -> tuple[str, list[dict]]:
    """회의 기록은 저장본(worklog)에서, 이슈·커밋은 GitHub에서 읽는다. Notion은 다시 읽지 않는다."""
    github_token = config["github_token"]
    repo = config["github_repo"]

    async def _fetch_issues() -> dict | list[dict]:
        return await _github_get_cached(repo, "/issues", github_token, {"state": "all", "per_page": 30})

    notes, issues = await asyncio.gather(_stored_notes(config, team_key, on_stage), _fetch_issues())
    note_lines = [text.strip() for text in notes["texts"] if text.strip()]
    footer = _note_footer(notes)
    issue_state_by_number: dict[int, str] = {i["number"]: i["state"] for i in issues if "pull_request" not in i}

    unassigned_open = [
        i for i in issues
        if "pull_request" not in i and i["state"] == "open" and not (i.get("assignees") or [])
    ][:10]
    unassigned_hint = ["", "🙋 담당자 없는 이슈가 있어요 — 아래 카드에서 맡을 수 있어요"] if unassigned_open else []

    if not note_lines:
        by_assignee: dict[str, list[str]] = {}
        for i in issues:
            if "pull_request" in i:
                continue
            for a in i.get("assignees") or [{"login": "미배정"}]:
                by_assignee.setdefault(a["login"], []).append(f"{i['title']} [{i['state']}]")
        lines = [f"{name}: {', '.join(tasks)}" for name, tasks in by_assignee.items()]
        result = ["👥 팀 진행상황", "\n".join(lines) if lines else "(회의록도 없고 등록된 이슈도 없어요)"]
        return "\n".join(result + unassigned_hint + footer), unassigned_open

    classified = await _classify_meeting_notes(note_lines, config["meeting_notes_page_id"])
    confirmed, undecided = classified["confirmed"], classified["undecided"]

    if not confirmed and not undecided:
        text = "👥 팀 진행상황 (요약 실패, 회의록 원문)\n" + "\n".join(f"- {_brief(t)}" for t in note_lines[-10:])
        return "\n".join([text] + unassigned_hint + footer), unassigned_open

    commit_touched = await _get_commit_touched_issues(repo, github_token)
    tasks = _classify_tasks_by_github_state(confirmed, issue_state_by_number, commit_touched)

    result = ["👥 팀 진행상황 (회의록 기준)"]
    if tasks["done"]:
        result += ["", "✅ 완료", *[f"- {t}" for t in tasks["done"]]]
    if tasks["in_progress"]:
        result += ["", "🔨 진행중 (커밋 있음)", *[f"- {t}" for t in tasks["in_progress"]]]
    if tasks["not_started"]:
        result += ["", "📋 대기 (이슈만 등록, 착수 전)", *[f"- {t}" for t in tasks["not_started"]]]
    if tasks["unlinked"]:
        result += ["", "🆕 이슈 미등록 (회의에서만 언급됨)", *[f"- {t}" for t in tasks["unlinked"]]]
    if undecided:
        result += ["", "🤔 아직 정해야 할 사안", *[f"- {t}" for t in undecided[:10]]]
    result += unassigned_hint + footer

    return "\n".join(result), unassigned_open


# ── 3. 내 역할 조회 ─────────────────────────────
async def fetch_my_role(github_id: str | None, display_name: str | None, config: dict, team_key: str,
                        on_stage: Callable[[str], None] | None = None) -> str:
    """배정된 이슈는 GitHub에서, 회의록 속 내 이름은 저장본(worklog)에서 찾는다. Notion은 다시 읽지 않는다."""
    github_token = config["github_token"]

    async def _fetch_github_lines() -> tuple[list[str], set[int]]:
        if not github_id:
            return [], set()
        issues = await _github_get_cached(
            config["github_repo"], "/issues", github_token, {"assignee": github_id, "state": "open", "per_page": 30}
        )
        lines: list[str] = []
        numbers: set[int] = set()
        for issue in issues:
            if "pull_request" in issue:
                continue
            lines.append(f"- {issue['title']} (#{issue['number']})")
            numbers.add(issue["number"])
        return lines, numbers

    async def _fetch_notes() -> dict:
        if not display_name:
            return {"texts": [], "as_of": None}
        return await _stored_notes(config, team_key, on_stage)

    (github_lines, github_issue_numbers), notes = await asyncio.gather(_fetch_github_lines(), _fetch_notes())
    # 원문 한 건의 첫 줄은 "[역할] 경로" 머리줄이라 빼고, 내용 줄 단위로 내 이름을 찾는다.
    raw_note_lines = [line for text in notes["texts"] for line in text.splitlines()[1:]]
    if not github_lines:
        github_lines.append("- (배정된 GitHub 이슈가 없어요)")

    meeting_lines: list[str] = []
    if display_name:
        for line in raw_note_lines:
            stripped = line.strip()
            if not stripped or display_name not in stripped:
                continue
            referenced = {int(m.group(1)) for m in _ISSUE_REF_RE.finditer(stripped)}
            if referenced & github_issue_numbers:
                continue
            meeting_lines.append(f"- {stripped}")

    meeting_lines = list(dict.fromkeys(meeting_lines))  # 같은 줄이 여러 기록에 있으면 한 번만
    result = ["👤 내 역할", "📌 GitHub 담당 이슈", *github_lines]
    if meeting_lines:
        result += ["📝 회의록 언급", *meeting_lines]
    return "\n".join(result + _note_footer(notes))


# ── 4. 완료된 작업 공유 ─────────────────────────────
async def _find_project_status_field(
    owner: str, repo_name: str, project_number: int, token: str
) -> tuple[str, str, str] | None:
    cache_key = (f"{owner}/{repo_name}", project_number)
    if cache_key in _project_status_field_cache:
        return _project_status_field_cache[cache_key]

    query = """
    query($owner: String!, $repo: String!, $number: Int!) {
      repository(owner: $owner, name: $repo) {
        projectV2(number: $number) {
          id
          fields(first: 20) {
            nodes {
              ... on ProjectV2SingleSelectField { id name options { id name } }
            }
          }
        }
      }
    }
    """
    data = await _github_graphql(query, token, {"owner": owner, "repo": repo_name, "number": project_number})
    project = data["repository"]["projectV2"]
    result: tuple[str, str, str] | None = None
    if project:
        for fld in project["fields"]["nodes"]:
            if fld.get("name") not in ("Status", "상태"):
                continue
            for option in fld.get("options", []):
                if option["name"] in ("Done", "완료"):
                    result = (project["id"], fld["id"], option["id"])
                    break
            if result:
                break

    _project_status_field_cache[cache_key] = result
    return result


async def _find_project_item_id(
    owner: str, repo_name: str, project_id: str, issue_number: int, token: str
) -> str | None:
    cache_key = (project_id, issue_number)
    if cache_key in _project_item_id_cache:
        return _project_item_id_cache[cache_key]

    query = """
    query($owner: String!, $repo: String!, $number: Int!) {
      repository(owner: $owner, name: $repo) {
        issue(number: $number) {
          projectItems(first: 10) {
            nodes { id project { id } }
          }
        }
      }
    }
    """
    data = await _github_graphql(query, token, {"owner": owner, "repo": repo_name, "number": issue_number})
    issue = data["repository"]["issue"]
    result = None
    if issue:
        for item in issue["projectItems"]["nodes"]:
            if item["project"]["id"] == project_id:
                result = item["id"]
                break

    _project_item_id_cache[cache_key] = result
    return result


async def _set_project_item_status(project_id: str, field_id: str, option_id: str, item_id: str, token: str) -> None:
    mutation = """
    mutation($projectId: ID!, $itemId: ID!, $fieldId: ID!, $optionId: String!) {
      updateProjectV2ItemFieldValue(
        input: { projectId: $projectId, itemId: $itemId, fieldId: $fieldId, value: { singleSelectOptionId: $optionId } }
      ) { projectV2Item { id } }
    }
    """
    await _github_graphql(
        mutation, token, {"projectId": project_id, "itemId": item_id, "fieldId": field_id, "optionId": option_id}
    )


async def share_completed_work(config: dict, github_id: str) -> str:
    repo = config["github_repo"]
    owner, repo_name = repo.split("/", 1)
    project_number = config["github_project_number"]
    token = config["github_token"]

    my_issues = await _github_get_cached(repo, "/issues", token, {"assignee": github_id, "state": "all", "per_page": 30})
    my_issue_numbers = {i["number"] for i in my_issues if "pull_request" not in i}
    if not my_issue_numbers:
        return "✅ 완료된 작업\n담당된 GitHub 이슈가 없어요."

    commits = await _github_get_cached(repo, "/commits", token, {"per_page": 20})
    touched_numbers: set[int] = set()
    for commit in commits:
        message = commit.get("commit", {}).get("message", "")
        touched_numbers |= {int(m.group(1)) for m in _ISSUE_REF_RE.finditer(message)}

    issue_numbers = sorted(my_issue_numbers & touched_numbers)
    if not issue_numbers:
        return "✅ 완료된 작업\n담당 이슈 중 최근 커밋에서 언급된 게 없어요."

    try:
        status_field = await _find_project_status_field(owner, repo_name, project_number, token)
    except Exception as exc:
        return (
            "⚠️ GitHub Project 정보를 가져오지 못했어요.\n"
            "토큰에 read:project / project 권한이 있는지 확인해주세요.\n"
            f"(에러: {exc})"
        )

    if status_field is None:
        return (
            f"⚠️ 연결된 GitHub Project(#{project_number})에서 'Status' 필드(Done/완료 옵션 포함)를 찾지 못했어요.\n"
            "Project에 Status 필드와 Done 옵션이 있는지 확인해주세요."
        )
    project_id, field_id, done_option_id = status_field

    updated: list[str] = []
    skipped: list[str] = []
    for number in issue_numbers:
        item_id = await _find_project_item_id(owner, repo_name, project_id, number, token)
        if item_id is None:
            skipped.append(f"#{number}")
            continue
        await _set_project_item_status(project_id, field_id, done_option_id, item_id, token)
        updated.append(f"#{number}")

    lines = ["✅ 완료된 작업으로 반영됨", ", ".join(updated) if updated else "(변경된 항목 없음)"]
    if skipped:
        lines.append(f"⏭️ Project에 없어서 건너뜀: {', '.join(skipped)}")
    return "\n".join(lines)


# ── 담당자 없는 이슈 self-claim ─────────────────────────────
async def claim_issue(config: dict, issue_number: int, github_id: str) -> str:
    """직전에 캐시 없이 다시 조회한다 — 두 사람이 동시에 눌러도 중복 배정되지 않게."""
    repo = config["github_repo"]
    token = config["github_token"]

    try:
        issue = await _github_get(repo, f"/issues/{issue_number}", token)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return f"⚠️ #{issue_number} 이슈를 찾을 수 없어요."
        raise

    existing_assignees = issue.get("assignees") or []
    if existing_assignees:
        names = ", ".join(a["login"] for a in existing_assignees)
        return f"⚠️ #{issue_number}는 이미 담당자가 있어요: {names}"

    await _github_patch(repo, f"/issues/{issue_number}", token, {"assignees": [github_id]})
    return f"🙋 #{issue_number} '{issue['title']}' 담당자로 등록됐어요!"


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=SERVER_PORT)
