"""카드 흐름 검증 도구: 지어낸 번호 사용자로 봇 턴을 순서대로 보내고, 각 단계의 서버 이벤트(JSON)·Teams 카드(JSON)·화면 캡처(PNG)를 남긴다.

Playground는 사용자 이름을 바꿀 수 없어서(`agentsplayground start --help`에 옵션 없음) `User 901` 같은 번호 사용자로 들어갈 수 없다.
그래서 이 도구는 봇이 서버에 보내는 것과 같은 턴(`/api/turn`)을 직접 보내고, 서버가 돌려준 카드 이벤트를 **봇의 카드 생성 코드
(`src/bot/main.py`의 `_build_buttons_card`)로 그대로** Adaptive Card JSON으로 바꾼다. 화면 캡처는 그 JSON을 간단한 렌더러로 그려 찍는다.

안전장치:
- 실제 저장소·설정을 쓰지 않는다: 임시 폴더의 DB(`WORKLOG_STORE`)와 임시 설정 파일(`TEAM_CONFIG_PATH` 바꿔 끼움)만 쓴다.
- 바깥 네트워크를 막는다(루프백만 허용). GitHub·Notion 확인은 가짜 함수로 바꾼다: 토큰이 `ghp_ok`/`ntn_ok`로 시작하면 통과.
- 결과물에 토큰·번호를 남기지 않는다: 저장하는 JSON에서 비밀 입력값은 `***`로 가린다.

사용법(저장소 루트에서):
    .venv/Scripts/python.exe scripts/verify_cards.py <시나리오> <출력 폴더>
    시나리오: B-before(변경 전 팀원 등록), A(처음 온 팀원 전체), A-no-github, B(연결된 팀의 두 번째 팀원), blocked(명단에 없는 사람),
             A-playground(개발용 번호 대체: Playground 기본 사용자 Alex Wilber가 개발용 팀으로 들어간다)
    A-playground는 개발용 번호를 환경변수로 넘긴다(번호는 이 파일에 쓰지 않는다):
    SEAM_ENV=development SEAM_DEV_MEMBER_NO=<roster.playground.json의 번호> .venv/Scripts/python.exe scripts/verify_cards.py A-playground <출력 폴더>
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
CHROME = Path(r"C:/Program Files/Google/Chrome/Application/chrome.exe")
SECRET_FIELDS = {"notion_token", "github_token"}

# ── 바깥 네트워크 차단(루프백만 허용) ─────────────────────────────
_ALLOWED = {"127.0.0.1", "::1", "localhost", "testserver", ""}
blocked: list[str] = []
_real_create = socket.create_connection
_real_getaddrinfo = socket.getaddrinfo


def _guard(host) -> None:
    host = str(host[0] if isinstance(host, tuple) else host)
    if host not in _ALLOWED and not host.startswith("127."):
        blocked.append(host)
        raise OSError(f"network blocked by verify_cards: {host}")


socket.create_connection = lambda address, *a, **k: (_guard(address), _real_create(address, *a, **k))[1]
socket.getaddrinfo = lambda host, *a, **k: (_guard(host) if host is not None else None, _real_getaddrinfo(host, *a, **k))[1]

# ── 임시 환경 ─────────────────────────────────────────────
WORK = Path(tempfile.mkdtemp(prefix="verify-cards-"))
os.environ.update({"WORKLOG_STORE": str(WORK / "w.sqlite3"), "BOT_SERVER_SECRET": "verify-secret", "WORKLOG_LOOP": "0",
                   "MEMBER_ONBOARDING": "1", "GROQ_API_KEY": "", "CEREBRAS_API_KEY": "", "ENV_FILE": str(WORK / "none.env")})
sys.path.insert(0, str(ROOT / "src" / "server"))
import main  # noqa: E402
import worklog_bridge  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

_spec = importlib.util.spec_from_file_location("bot_main", ROOT / "src" / "bot" / "main.py")
bot = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bot)

CONFIG_PATH = WORK / "team_configs.json"
CONFIG_PATH.write_text("{}", encoding="utf-8")


def seed_config(key: str, config: dict) -> None:
    data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    data[key] = config
    CONFIG_PATH.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def seed_roster(rows: list[dict]) -> None:
    """명단(지어낸 번호). 마이그레이션 10이 있는 코드에서만 쓴다."""
    from worklog import teams  # noqa: PLC0415

    teams.load_roster(Path(os.environ["WORKLOG_STORE"]), rows)


# ── 가짜 바깥 확인 ─────────────────────────────────────────
async def fake_github_token_valid(token: str) -> bool:
    return token.startswith("ghp_ok")


def fake_notion_accessible(page_id: str, client) -> bool:
    return True


def fake_notion_client(token: str, background: bool = False):
    class Pages:
        def retrieve(self, page_id):
            if not token.startswith("ntn_ok"):
                raise PermissionError("unauthorized")
            return {"id": page_id, "last_edited_time": "2026-11-01T00:00:00Z",
                    "properties": {"title": {"type": "title", "title": [{"plain_text": "캠퍼스 분실물 찾기"}]}}}

    class Client:
        pages = Pages()

    return Client()


# ── 한 턴 보내기·캡처 ──────────────────────────────────────
class Recorder:
    def __init__(self, client: TestClient, user: dict, out: Path):
        self.client, self.user, self.out = client, user, out
        self.steps: list[dict] = []
        self.last_buttons: list[dict] = []

    def turn(self, label: str, *, kind: str = "message", text: str = "", value: dict | None = None) -> list[dict]:
        body = {"kind": kind, "user": self.user, "team": None, "text": text, "value": value,
                "conversation": {"id": "verify-conv", "type": "personal"}, "message_id": f"m{len(self.steps)}",
                "sent_at": "2026-11-09T00:00:00Z", "attachments": []}
        resp = self.client.post("/api/turn", json=body, headers={"X-Bot-Secret": "verify-secret"})
        events = [json.loads(line) for line in resp.text.splitlines() if line.strip()]
        cards = []
        for event in events:
            if event["type"] in ("buttons", "replace"):
                card = bot._build_buttons_card(event).model_dump(by_alias=True, exclude_none=True)
                cards.append({"event": event["type"], "card": card})
                if event["type"] == "buttons":
                    self.last_buttons = event["buttons"]
        sent = {k: ("***" if k in SECRET_FIELDS else v) for k, v in (value or {}).items()}
        self.steps.append({"label": label, "sent": {"text": text, "value": sent}, "events": events, "cards": cards})
        return events

    def click(self, label: str, title_part: str, inputs: dict | None = None) -> list[dict]:
        button = next((b for b in self.last_buttons if title_part in b["title"]), None)
        if button is None:
            raise SystemExit(f"[{label}] '{title_part}' 버튼이 없어요: {[b['title'] for b in self.last_buttons]}")
        return self.turn(label, value={**button["data"], **(inputs or {})})

    def save(self) -> None:
        self.out.mkdir(parents=True, exist_ok=True)
        masked = json.loads(json.dumps(self.steps, ensure_ascii=False))
        _mask(masked)
        (self.out / "steps.json").write_text(json.dumps(masked, ensure_ascii=False, indent=2), encoding="utf-8")
        for n in range(len(masked)):
            page = self.out / f"step{n + 1:02d}.html"
            page.write_text(render(masked[: n + 1]), encoding="utf-8")
            if CHROME.exists():
                subprocess.run([str(CHROME), "--headless=new", "--disable-gpu", "--hide-scrollbars", "--window-size=520,3200",
                                f"--screenshot={self.out / f'step{n + 1:02d}.png'}", page.as_uri()],
                               capture_output=True, timeout=60)
        print(json.dumps({"out": str(self.out), "steps": [s["label"] for s in masked], "blocked_network": blocked},
                         ensure_ascii=False, indent=2))


_TOKEN_LIKE = __import__("re").compile(r"^(ghp_|github_pat_|ntn_|secret_)[A-Za-z0-9_]{6,}$")


def _mask(node) -> None:
    """저장 전 한 번 더: 비밀 입력칸 값과 토큰처럼 생긴 문자열을 가린다."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key in SECRET_FIELDS and isinstance(value, str):
                node[key] = "***"
            elif isinstance(value, str) and _TOKEN_LIKE.match(value):
                node[key] = "***"
            else:
                _mask(value)
    elif isinstance(node, list):
        for item in node:
            _mask(item)


# ── 아주 작은 Adaptive Card 렌더러(캡처용) ─────────────────────────
def _esc(text) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _element(el: dict) -> str:
    kind = el.get("type")
    if kind == "TextBlock":
        weight = "font-weight:700;" if el.get("weight") == "Bolder" else ""
        size = "font-size:18px;" if el.get("size") in ("Large", "ExtraLarge") else ""
        return f'<p style="margin:6px 0;{weight}{size}white-space:pre-wrap">{_esc(el.get("text", ""))}</p>'
    if kind == "Input.Text":
        shown = "••••••••" if el.get("style") == "Password" and el.get("value") else _esc(el.get("value") or "")
        hint = _esc(el.get("placeholder") or "")
        style = " <small>(가림 입력)</small>" if el.get("style") == "Password" else ""
        return (f'<label style="display:block;margin:8px 0 2px;font-size:12px;color:#555">{_esc(el.get("label") or el.get("id"))}{style}</label>'
                f'<div style="border:1px solid #bbb;border-radius:4px;padding:6px;color:#999;min-height:30px">{shown or hint}</div>')
    if kind == "Input.ChoiceSet":
        return f'<div style="border:1px solid #bbb;border-radius:4px;padding:6px;margin:6px 0">▾ {_esc(el.get("placeholder") or "")}</div>'
    if kind == "Input.Toggle":
        return f'<p>☐ {_esc(el.get("title", ""))}</p>'
    if kind == "Image":
        return f'<p style="color:#888">[그림: {_esc(el.get("altText", ""))}]</p>'
    if kind == "Container":
        return "".join(_element(item) for item in el.get("items", []))
    return f'<p style="color:#b00">[{_esc(kind)}]</p>'


def _card(card: dict, kind: str) -> str:
    body = "".join(_element(el) for el in card.get("body", []))
    actions = "".join(
        f'<span style="display:inline-block;border:1px solid #6264a7;color:#6264a7;border-radius:4px;padding:5px 10px;margin:4px 4px 0 0">'
        f'{_esc(a.get("title", ""))}</span>' for a in card.get("actions", []))
    tag = "카드 교체" if kind == "replace" else f'Adaptive Card {card.get("version", "")}'
    return (f'<div style="border:1px solid #ddd;border-radius:8px;padding:10px;margin:6px 0;background:#fff">'
            f'<div style="font-size:11px;color:#888">{tag}</div>{body}<div>{actions}</div></div>')


def render(steps: list[dict]) -> str:
    rows = []
    for step in steps:
        sent = step["sent"]
        said = sent["text"] or (json.dumps(sent["value"], ensure_ascii=False) if sent["value"] else "(봇 열기)")
        rows.append(f'<div style="text-align:right;margin:10px 0"><span style="background:#e9e8f5;border-radius:8px;padding:6px 10px;display:inline-block">'
                    f'<small style="color:#888">{_esc(step["label"])}</small><br>{_esc(said)}</span></div>')
        cards = iter(step["cards"])
        streamed = "".join(e["text"] for e in step["events"] if e["type"] == "chunk")
        if streamed:  # 스트리밍 글(메뉴 1 요약 등)은 이어 붙여 한 말풍선으로
            rows.append(f'<div style="background:#f4f4f4;border-radius:8px;padding:8px 10px;margin:6px 0;white-space:pre-wrap">{_esc(streamed)}</div>')
        for event in step["events"]:
            if event["type"] == "message":
                rows.append(f'<div style="background:#f4f4f4;border-radius:8px;padding:8px 10px;margin:6px 0;white-space:pre-wrap">{_esc(event["text"])}</div>')
            elif event["type"] in ("buttons", "replace"):
                item = next(cards)
                rows.append(_card(item["card"], item["event"]))
    return ('<meta charset="utf-8"><body style="font:14px \'Malgun Gothic\',sans-serif;background:#f0f0f0;padding:12px;width:480px">'
            + "".join(rows) + "</body>")


# ── 시나리오 ───────────────────────────────────────────────
def scenario_b_before(rec: Recorder) -> None:
    """변경 전: 설정이 있는 팀(지금 코드는 1:1 채팅이면 user:<ID> 범위)에서 역할 없는 팀원이 처음 들어온다."""
    seed_config(f"user:{rec.user['aad_id']}", {"github_repo": "demo-org/demo-repo", "github_token": "ghp_ok_seed",
                                               "notion_token": "ntn_ok_seed", "notion_page_id": "0" * 32,
                                               "github_project_number": 1, "admin_aad_id": "someone-else"})
    rec.turn("1. 봇 열기", kind="welcome")
    rec.turn("2. 메뉴 1 누르기", value={"action": "menu", "choice": "1"})
    rec.click("3. 역할 고르기", "개발")
    rec.click("4. GitHub 건너뛰기", "건너뛰기")
    rec.click("5. 별칭 건너뛰기", "건너뛰기")


def scenario_a(rec: Recorder, github: bool = True) -> None:
    seed_roster([{"member_no": "901", "team_code": "alpha", "team_name": "알파팀", "is_leader": False},
                 {"member_no": "902", "team_code": "alpha", "team_name": "알파팀", "is_leader": False}])
    rec.turn("1. 봇 열기", kind="welcome")
    _after_welcome(rec, github)


def _after_welcome(rec: Recorder, github: bool = True) -> None:
    """팀 인사 다음부터. '이 팀이 맞나요?' 화면은 뺐다(노션 확인 → 바로 GitHub 단계)."""
    rec.click("2. 팀 연결 시작", "팀 연결 시작")
    rec.click("3. 노션 틀린 토큰", "연결하기", {"notion_link": "https://www.notion.so/demo-" + "a" * 32, "notion_token": "abc"})
    rec.click("4. 노션 연결", "연결하기", {"notion_link": "https://www.notion.so/demo-" + "a" * 32, "notion_token": "ntn_ok_demo123"})
    if not github:
        rec.click("5. GitHub 나중에", "나중에")
        rec.click("6. 팀 현황 보기", "팀 현황")
        return
    rec.click("5. GitHub 연결 선택", "연결하기")
    rec.click("6. GitHub 연결", "연결하기", {"github_repo": "https://github.com/demo-org/demo-repo", "github_token": "ghp_ok_demo123"})
    rec.click("7. 보드 만들기", "만들어 주세요")
    rec.click("8. 팀 현황 보기", "팀 현황")


def scenario_a_playground(rec: Recorder) -> None:
    """개발용 번호 대체. 같은 번호가 실제 명단 쪽 팀(여기서는 지어낸 alpha)에 있어도 개발용 팀으로만 간다."""
    if not main._DEV_OVERRIDE:
        raise SystemExit("SEAM_ENV=development, SEAM_DEV_MEMBER_NO를 주고 실행해 주세요")
    seed_roster([{"member_no": os.environ["SEAM_DEV_MEMBER_NO"], "team_code": "alpha", "team_name": "알파팀", "is_leader": False}])
    rec.turn("1. 봇 열기", kind="welcome")
    _after_welcome(rec, github=False)
    from worklog import teams  # noqa: PLC0415

    store = Path(os.environ["WORKLOG_STORE"])
    dev_scope = teams.scope_of(main._DEV_OVERRIDE["code"])
    rec.out.mkdir(parents=True, exist_ok=True)
    (rec.out / "check.json").write_text(json.dumps({  # 어느 팀이 연결됐는지(번호는 넣지 않는다)
        "dev_team_scope": dev_scope, "dev_team_status": teams.get(store, dev_scope)["status"],
        "alpha_team_status": teams.get(store, "squad:alpha")["status"],
        "saved_config_scopes": sorted(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))}, ensure_ascii=False, indent=2),
        encoding="utf-8")


def scenario_b(rec: Recorder) -> None:
    """변경 후: 이미 연결된 팀(명단 기준)의 두 번째 팀원."""
    seed_roster([{"member_no": "901", "team_code": "alpha", "team_name": "알파팀", "is_leader": False},
                 {"member_no": "902", "team_code": "alpha", "team_name": "알파팀", "is_leader": False}])
    from worklog import teams  # noqa: PLC0415

    store = Path(os.environ["WORKLOG_STORE"])
    teams.begin_setup(store, "squad:alpha", "aad-901")
    teams.finish_setup(store, "squad:alpha", "aad-901", lambda: seed_config("squad:alpha", {
        "notion_token": "ntn_ok_seed", "notion_page_id": "0" * 32, "team_name": "알파팀", "admin_aad_id": "aad-901"}))
    rec.turn("1. 봇 열기", kind="welcome")
    rec.turn("2. 메뉴 1 누르기", value={"action": "menu", "choice": "1"})
    rec.click("3. 역할 고르기", "개발")
    rec.click("4. GitHub 건너뛰기", "건너뛰기")
    rec.click("5. 별칭 건너뛰기", "건너뛰기")


def scenario_blocked(rec: Recorder) -> None:
    seed_roster([{"member_no": "901", "team_code": "alpha", "team_name": "알파팀", "is_leader": False}])
    rec.turn("1. 봇 열기", kind="welcome")
    rec.turn("2. 메뉴 1 누르기", value={"action": "menu", "choice": "1"})
    rec.turn("3. 아무 말", text="안녕")


SCENARIOS = {
    "B-before": (scenario_b_before, {"aad_id": "aad-902", "name": "User 902"}),
    "A": (scenario_a, {"aad_id": "aad-901", "name": "User 901"}),
    "A-no-github": (lambda r: scenario_a(r, github=False), {"aad_id": "aad-901", "name": "User 901"}),
    "B": (scenario_b, {"aad_id": "aad-902", "name": "User 902"}),
    "blocked": (scenario_blocked, {"aad_id": "aad-999", "name": "User 999"}),
    "A-playground": (scenario_a_playground, {"aad_id": "aad-playground", "name": "Alex Wilber"}),
}


def main_cli() -> None:
    name, out = sys.argv[1], Path(sys.argv[2]).resolve()
    if out.exists():
        shutil.rmtree(out)
    run, user = SCENARIOS[name]
    patches = [patch.object(main, "TEAM_CONFIG_PATH", str(CONFIG_PATH)),
               patch.object(main, "MEMBER_PROFILE_PATH", str(WORK / "member_profiles.json")),
               patch.object(main, "_github_token_valid", fake_github_token_valid),
               patch.object(main, "_notion_page_accessible", fake_notion_accessible),
               patch.object(main, "notion_client_for", fake_notion_client),
               patch.object(main, "_register_team_map", AsyncMock()),
               patch.object(main, "_check_on_open", lambda *a, **k: None),
               patch.object(main, "_first_read", AsyncMock()),
               patch.object(worklog_bridge, "team_snapshot", lambda *a, **k: {"text": "검증용 팀 소개 원문", "as_of": None}),
               patch.object(main, "_summarize_project_info", AsyncMock(return_value="(검증용 요약)"))]
    # 팀 연결 흐름의 GitHub 확인(가짜): 저장소는 열리고, 보드는 없어서 만들어 준다
    for name, fake in (("_github_repo_ok", AsyncMock(return_value=True)), ("_github_find_board", AsyncMock(return_value=None)),
                       ("_github_create_board", AsyncMock(return_value={"id": "PVT_demo", "number": 7}))):
        if hasattr(main, name):
            patches.append(patch.object(main, name, fake))
    for p in patches:
        p.start()
    try:
        rec = Recorder(TestClient(main.app), user, out)
        run(rec)
        rec.save()
    finally:
        for p in patches:
            p.stop()


if __name__ == "__main__":
    main_cli()
    asyncio.set_event_loop(None)
