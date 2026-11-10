"""처음 온 팀원(시나리오 A): 명단으로 팀 찾기, 팀 연결 카드 흐름, 동시 설정, 토큰·번호 누출, GitHub 없이 쓰기.
문서: docs/seam/01-setup.md. 모든 번호·토큰·이름은 지어낸 값이다."""

from __future__ import annotations

import isolation  # noqa: F401,I001  실제 저장소·키를 쓰지 않게(맨 먼저)

import asyncio
import importlib.util
import json
import logging
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402
import setup_flow  # noqa: E402
import worklog_bridge  # noqa: E402
from worklog import db, teams  # noqa: E402

NOTION_TOKEN = "ntn_ok_FAKEVALUE0001"
GITHUB_TOKEN = "ghp_ok_FAKEVALUE0002"
ROSTER = [{"member_no": "9071", "team_code": "alpha", "team_name": "알파팀", "is_leader": True},
          {"member_no": "9072", "team_code": "alpha", "team_name": "알파팀", "is_leader": False},
          {"member_no": "9081", "team_code": "beta", "team_name": "베타팀", "is_leader": True}]
PAGE = "https://www.notion.so/demo-" + "b" * 32


def fake_notion_client(token: str, background: bool = False):
    class Pages:
        def retrieve(self, page_id):
            if not token.startswith("ntn_ok"):
                raise PermissionError("unauthorized")
            return {"properties": {"title": {"type": "title", "title": [{"plain_text": "데모 프로젝트"}]}}}

    class Databases:
        def retrieve(self, page_id):
            raise PermissionError("unauthorized")

    class Client:
        pages = Pages()
        databases = Databases()

    return Client()


class SetupTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.store = root / "w.sqlite3"
        self.config_path = root / "team_configs.json"
        # 기존 데이터: 예전 1:1 범위와 팀 채널 범위 설정. 이 테스트들은 이 값이 바뀌지 않는지도 본다
        self.existing = {"user:aad-old": {"notion_token": "ntn_ok_OLDVALUE", "notion_page_id": "1" * 32},
                         "team:legacy": {"github_repo": "o/r", "github_token": "ghp_ok_OLD", "notion_token": "ntn_ok_OLD2",
                                         "notion_page_id": "2" * 32, "admin_aad_id": "someone"}}
        self.config_path.write_text(json.dumps(self.existing), encoding="utf-8")
        self.github_valid = AsyncMock(side_effect=lambda token: token.startswith("ghp_ok"))
        self.find_board = AsyncMock(return_value=None)
        self.create_board = AsyncMock(return_value={"id": "PVT_x", "number": 3})
        self.patches = [patch.object(worklog_bridge, "WORKLOG_STORE", self.store),
                        patch.object(main, "TEAM_CONFIG_PATH", str(self.config_path)),
                        patch.object(main, "MEMBER_PROFILE_PATH", str(root / "member_profiles.json")),
                        patch.object(main, "_github_token_valid", self.github_valid),
                        patch.object(main, "notion_client_for", fake_notion_client),
                        patch.object(main, "_github_repo_ok", AsyncMock(return_value=True)),
                        patch.object(main, "_github_find_board", self.find_board),
                        patch.object(main, "_github_create_board", self.create_board),
                        patch.object(main, "_register_team_map", AsyncMock()),
                        patch.object(main, "_check_on_open", lambda *a, **k: None),
                        patch.object(main, "_first_read", AsyncMock()),
                        patch.object(worklog_bridge, "team_snapshot", lambda *a, **k: {"text": "팀 소개", "as_of": None}),
                        patch.object(main, "_github_get_cached", AsyncMock(return_value=[])),
                        patch.object(main, "_get_commit_touched_issues", AsyncMock(return_value=set())),
                        patch.object(main, "_summarize_project_info", AsyncMock(return_value="요약"))]
        for p in self.patches:
            p.start()
        self.client = TestClient(main.app)

    def tearDown(self) -> None:
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def roster(self) -> None:
        teams.load_roster(self.store, ROSTER)

    def turn(self, name: str, aad: str, *, kind: str = "message", text: str = "", value: dict | None = None) -> list[dict]:
        body = {"kind": kind, "user": {"aad_id": aad, "name": name}, "team": None, "text": text, "value": value,
                "conversation": {"id": f"c-{aad}", "type": "personal"}, "message_id": "m", "sent_at": "2026-11-09T00:00:00Z"}
        resp = self.client.post("/api/turn", json=body, headers={"X-Bot-Secret": "test-secret"})
        self.assertEqual(resp.status_code, 200)
        return [json.loads(line) for line in resp.text.splitlines() if line.strip()]

    def configs(self) -> dict:
        return json.loads(self.config_path.read_text(encoding="utf-8"))

    def team(self, scope: str = "squad:alpha") -> dict:
        return teams.get(self.store, scope)

    def connect(self, name: str = "User 9071", aad: str = "aad-1") -> list[list[dict]]:
        out = [self.turn(name, aad, value={"action": "setup_start"}),
               self.turn(name, aad, value={"action": "setup_notion", "notion_link": PAGE, "notion_token": NOTION_TOKEN})]
        return out


class RosterAndMigrationTests(SetupTestCase):
    def test_migration_and_roster_load_are_repeatable(self) -> None:
        db.initialize(self.store)
        db.initialize(self.store)  # 두 번 실행해도 결과가 같다
        with closing(sqlite3.connect(self.store)) as c:
            version = c.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()[0]
            tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertEqual(int(version), db.SCHEMA_VERSION)
        self.assertTrue({"teams", "roster", "team_config_log"} <= tables)
        first = teams.load_roster(self.store, ROSTER)
        second = teams.load_roster(self.store, ROSTER)
        self.assertEqual((first["teams_added"], second["teams_added"]), (2, 0))
        with closing(sqlite3.connect(self.store)) as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM roster").fetchone()[0], 3)
            self.assertNotIn("name", [r[1] for r in c.execute("PRAGMA table_info(roster)")])  # 명단에 이름 칸이 없다

    def test_roster_load_keeps_a_connected_team_as_it_is(self) -> None:
        self.roster()
        self.connect()
        teams.load_roster(self.store, ROSTER)
        self.assertEqual(self.team()["status"], "connected")

    def test_member_number_comes_only_from_the_user_display_name_format(self) -> None:
        self.assertEqual(teams.member_no("User 9071"), "9071")
        self.assertEqual(teams.member_no(" user 9071 "), "9071")
        for name in (None, "", "Alex Wilber", "User", "User 90 71", "9071", "User 9071 김"):
            self.assertIsNone(teams.member_no(name), name)


class ScenarioATests(SetupTestCase):
    def test_first_member_connects_notion_and_skips_github(self) -> None:
        self.roster()
        welcome = self.turn("User 9071", "aad-1", kind="welcome")
        self.assertEqual(welcome[0]["card"]["heading"], "알파팀이시네요 👋")
        start = self.turn("User 9071", "aad-1", value={"action": "setup_start"})
        inputs = {i["id"]: i for i in start[0]["card"]["inputs"]}
        self.assertEqual(inputs["notion_token"]["style"], "Password")
        bad = self.turn("User 9071", "aad-1", value={"action": "setup_notion", "notion_link": PAGE, "notion_token": "abc"})
        self.assertEqual(bad[0]["type"], "replace")  # 제출한 카드를 값 없는 카드로 바꾼다
        self.assertIn("토큰 형식이 아니에요", bad[1]["card"]["text"])
        good = self.turn("User 9071", "aad-1", value={"action": "setup_notion", "notion_link": PAGE, "notion_token": NOTION_TOKEN})
        self.assertEqual(good[0]["type"], "replace")
        # '이 팀이 맞나요?' 화면은 뺐다: 노션 확인이 끝나면 바로 저장하고 GitHub 단계로 간다
        self.assertEqual(good[1]["card"]["heading"], "2 / 2 · GitHub (선택)")
        self.assertFalse(any("이 팀이 맞나요" in json.dumps(e, ensure_ascii=False) for e in good))
        saved = self.configs()["squad:alpha"]
        self.assertEqual((saved["notion_token"], saved["team_name"]), (NOTION_TOKEN, "알파팀"))
        self.assertNotIn("github_repo", saved)
        later = self.turn("User 9071", "aad-1", value={"action": "setup_github_later"})
        self.assertEqual(later[0]["card"]["heading"], "준비 끝! 🎉")
        self.assertEqual(self.team()["status"], "connected")
        logged = teams.changes(self.store, "squad:alpha")
        self.assertEqual(sorted(r["field"] for r in logged), ["notion_page_id", "notion_token"])
        self.assertTrue(all(set(r) == {"field", "action", "actor", "changed_at"} for r in logged))  # 값은 기록하지 않는다

    def test_github_and_board_are_connected_after_the_team_is_connected(self) -> None:
        self.roster()
        self.connect()
        self.turn("User 9071", "aad-1", value={"action": "setup_github_open"})
        bad = self.turn("User 9071", "aad-1", value={"action": "setup_github", "github_repo": "demo/repo", "github_token": "ghp_bad_FAKE00009"})
        self.assertIn("로그인할 수 없어요", bad[1]["card"]["text"])
        self.turn("User 9071", "aad-1", value={"action": "setup_github", "github_repo": "https://github.com/demo/repo",
                                               "github_token": GITHUB_TOKEN})
        done = self.turn("User 9071", "aad-1", value={"action": "setup_board_create"})
        self.assertIn("작업 보드 연결됨", done[0]["card"]["text"])
        saved = self.configs()["squad:alpha"]
        self.assertEqual((saved["github_repo"], saved["github_project_number"]), ("demo/repo", 3))
        again = self.turn("User 9071", "aad-1", value={"action": "setup_board_create"})  # 두 번 눌러도 다시 만들지 않는다
        self.assertEqual(self.create_board.await_count, 1)
        self.assertEqual(again[0]["card"]["heading"], "준비 끝! 🎉")

    def test_submitting_twice_gives_the_same_result(self) -> None:
        self.roster()
        self.connect()
        before = self.config_path.read_bytes()
        again = self.turn("User 9071", "aad-1", value={"action": "setup_notion", "notion_link": PAGE, "notion_token": NOTION_TOKEN})
        self.assertEqual(again[0]["type"], "replace")  # 다시 낸 값도 지운다
        self.assertIn("이미 끝났어요", again[1]["text"])
        self.assertEqual(self.config_path.read_bytes(), before)
        self.assertEqual(len(teams.changes(self.store, "squad:alpha")), 2)

    def test_second_member_of_a_connected_team_goes_to_the_normal_menu(self) -> None:
        self.roster()
        self.connect()
        events = self.turn("User 9072", "aad-2", kind="welcome")
        self.assertIn("아래에서 골라주세요", events[-1]["title"])  # 연결된 팀: 예전 메뉴(시나리오 B는 그대로)

    def test_people_not_on_the_roster_are_blocked(self) -> None:
        self.roster()
        for name in ("User 1234", "Alex Wilber", None):
            events = self.turn(name, "aad-x", kind="welcome")
            self.assertEqual(len(events), 1)
            self.assertIn("어느 팀인지 찾지 못했어요", events[0]["text"])
        self.assertNotIn("1234", self.turn("User 1234", "aad-x", text="안녕")[0]["text"])

    def test_without_a_roster_the_old_behavior_is_kept(self) -> None:
        events = self.turn("Alex Wilber", "aad-old", kind="welcome")
        self.assertIn("아래에서 골라주세요", events[-1]["title"])  # 명단을 넣지 않은 설치는 예전 1:1 범위 그대로

    def test_existing_team_data_is_never_changed(self) -> None:
        self.roster()
        self.connect()
        self.turn("User 9081", "aad-3", value={"action": "setup_start"})
        configs = self.configs()
        for key, value in self.existing.items():
            self.assertEqual(configs[key], value)

    def test_menus_work_without_github(self) -> None:
        self.roster()
        self.connect()
        config = self.configs()["squad:alpha"]
        text, unassigned = asyncio.run(main.fetch_team_status(config, "squad:alpha"))
        self.assertEqual(unassigned, [])
        self.assertIn("팀 진행상황", text)
        self.assertEqual(asyncio.run(main.share_completed_work(config, "dev1", "squad:alpha")), main._NO_GITHUB_TEXT)
        self.assertEqual(asyncio.run(main.claim_issue(config, 1, "dev1")), main._NO_GITHUB_TEXT)
        role = asyncio.run(main.fetch_my_role("dev1", "User 9071", config, "squad:alpha"))
        self.assertIsInstance(role, str)


class ConcurrentSetupTests(SetupTestCase):
    def test_only_the_first_to_finish_is_saved(self) -> None:
        self.roster()
        self.turn("User 9071", "aad-1", value={"action": "setup_start"})
        busy = self.turn("User 9072", "aad-2", value={"action": "setup_start"})
        self.assertIn("다른 팀원이 지금 팀을 연결하고 있어요", busy[0]["text"])
        sneaky = self.turn("User 9072", "aad-2", value={"action": "setup_notion", "notion_link": PAGE, "notion_token": NOTION_TOKEN})
        self.assertEqual(sneaky[0]["type"], "replace")  # 진행자가 아니면 값을 받지 않고 지운다
        self.assertNotIn("squad:alpha", self.configs())
        self.turn("User 9071", "aad-1", value={"action": "setup_notion", "notion_link": PAGE, "notion_token": NOTION_TOKEN})
        self.assertEqual(self.configs()["squad:alpha"]["admin_aad_id"], "aad-1")

    def test_state_transitions(self) -> None:
        self.roster()
        store, scope = self.store, "squad:alpha"
        self.assertEqual(teams.begin_setup(store, scope, "a"), "started")
        self.assertEqual(teams.begin_setup(store, scope, "a"), "resumed")
        self.assertEqual(teams.begin_setup(store, scope, "b"), "busy")
        self.assertEqual(teams.finish_setup(store, scope, "b", lambda: self.fail("b must not write")), "lost")
        writes: list[str] = []
        self.assertEqual(teams.finish_setup(store, scope, "a", lambda: writes.append("a")), "connected")
        self.assertEqual(teams.finish_setup(store, scope, "a", lambda: writes.append("again")), "already")
        self.assertEqual(writes, ["a"])
        self.assertEqual(teams.begin_setup(store, scope, "b"), "connected")
        self.assertEqual(teams.begin_setup(store, "squad:nope", "a"), "unknown")

    def test_a_stale_setup_can_be_taken_over_and_the_old_one_cannot_finish(self) -> None:
        self.roster()
        store, scope = self.store, "squad:alpha"
        teams.begin_setup(store, scope, "a")
        with db.write(store) as c:
            c.execute("UPDATE teams SET setup_started_at = '2026-01-01T00:00:00Z' WHERE scope = ?", (scope,))
        self.assertEqual(teams.begin_setup(store, scope, "b"), "started")
        self.assertEqual(teams.finish_setup(store, scope, "a", lambda: self.fail("stale owner must not write")), "lost")
        self.assertEqual(teams.finish_setup(store, scope, "b", lambda: None), "connected")

    def test_two_finishes_racing_write_once(self) -> None:
        self.roster()
        store, scope = self.store, "squad:alpha"
        teams.begin_setup(store, scope, "a")
        writes: list[str] = []

        async def race():
            return await asyncio.gather(*(asyncio.to_thread(teams.finish_setup, store, scope, "a", lambda: writes.append("w"))
                                          for _ in range(5)))

        results = asyncio.run(race())
        self.assertEqual(sorted(results), ["already"] * 4 + ["connected"])
        self.assertEqual(writes, ["w"])


    def test_two_board_clicks_at_once_create_one_board(self) -> None:
        self.roster()
        self.connect()
        main._add_team_config("squad:alpha", {"github_repo": "demo/repo", "github_token": GITHUB_TOKEN})

        async def slow_create(repo, token, title):
            await asyncio.sleep(0.05)
            return {"id": "PVT_y", "number": 4}

        self.create_board.side_effect = slow_create

        class FakeTurn:
            def __init__(self, aad):
                self.aad_id, self.sent = aad, []

            async def send(self, text):
                self.sent.append(text)

            async def send_buttons(self, title, buttons, emphasis=False, card=None):
                self.sent.append(title)

            async def replace(self, heading, text):
                self.sent.append(text)

        team = teams.get(self.store, "squad:alpha")

        async def both():
            await asyncio.gather(*(setup_flow.handle(FakeTurn(a), team, {"action": "setup_board_create"}, main._setup_deps())
                                   for a in ("aad-1", "aad-2")))

        asyncio.run(both())
        self.assertEqual(self.create_board.await_count, 1)
        self.assertEqual(self.configs()["squad:alpha"]["github_project_number"], 4)

class RosterGuardTests(SetupTestCase):
    def test_a_number_already_in_another_team_is_not_moved(self) -> None:
        self.roster()
        moved = [{"member_no": "9072", "team_code": "beta", "team_name": "베타팀", "is_leader": False},
                 {"member_no": "9091", "team_code": "beta", "team_name": "베타팀", "is_leader": False}]
        with self.assertRaises(db.ValidationError) as caught:
            teams.load_roster(self.store, moved)
        self.assertNotIn("9072", str(caught.exception))
        with closing(sqlite3.connect(self.store)) as c:  # 아무것도 넣지 않았다(한 묶음으로 되돌림)
            rows = dict(c.execute("SELECT member_no, scope FROM roster"))
        self.assertEqual(rows, {"9071": "squad:alpha", "9072": "squad:alpha", "9081": "squad:beta"})

    def test_dev_team_codes_cannot_be_loaded_into_the_roster(self) -> None:
        with self.assertRaises(db.ValidationError):
            teams.load_roster(self.store, [{"member_no": "9095", "team_code": "dev-x", "team_name": "x"}])


class DevOverrideTests(SetupTestCase):
    """개발용 번호 대체. 실제 번호 대신 지어낸 번호(9099)를 쓴다."""

    def dev_file(self, rows: list[dict]) -> Path:
        path = Path(self.tmp.name) / "roster.playground.json"
        path.write_text(json.dumps(rows), encoding="utf-8")
        return path

    def test_only_a_development_environment_can_turn_it_on(self) -> None:
        self.assertIsNone(teams.dev_member({}))
        self.assertEqual(teams.dev_member({"SEAM_DEV_MEMBER_NO": "9099", "SEAM_ENV": "development"}), "9099")
        refused = [{"SEAM_DEV_MEMBER_NO": "9099"},
                   {"SEAM_DEV_MEMBER_NO": "9099", "SEAM_ENV": "production"},
                   {"SEAM_DEV_MEMBER_NO": "9099", "SEAM_ENV": "development", "CLIENT_ID": "x"},
                   {"SEAM_DEV_MEMBER_NO": "abc", "SEAM_ENV": "development"}]
        for environ in refused:
            with self.assertRaises(teams.DevOverrideError) as caught:
                teams.dev_member(environ)
            self.assertNotIn("9099", str(caught.exception))

    def test_the_server_refuses_to_start_outside_development(self) -> None:
        path = self.dev_file([{"member_no": "9099", "team_code": "play", "team_name": "놀이팀", "is_leader": True}])
        with patch.dict("os.environ", {"SEAM_DEV_MEMBER_NO": "9099", "SEAM_ENV": "production"}),                 patch.object(main, "DEV_ROSTER_PATH", path):
            with self.assertRaises(teams.DevOverrideError):
                main._load_dev_override()
        with patch.dict("os.environ", {"SEAM_DEV_MEMBER_NO": "9098", "SEAM_ENV": "development"}),                 patch.object(main, "DEV_ROSTER_PATH", path):
            with self.assertRaises(teams.DevOverrideError):  # 개발용 명단에 없는 번호
                main._load_dev_override()

    def test_playground_user_goes_to_the_dev_team_even_if_the_number_is_in_the_real_roster(self) -> None:
        # 실제 명단에 같은 번호가 다른 팀으로 있어도, 개발용 대체는 개발용 명단의 팀(squad:dev-…)으로만 간다
        teams.load_roster(self.store, ROSTER + [{"member_no": "9099", "team_code": "beta", "team_name": "베타팀"}])
        path = self.dev_file([{"member_no": "9099", "team_code": "play", "team_name": "놀이팀", "is_leader": True}])
        with patch.dict("os.environ", {"SEAM_DEV_MEMBER_NO": "9099", "SEAM_ENV": "development"}),                 patch.object(main, "DEV_ROSTER_PATH", path):
            override = main._load_dev_override()
        with patch.object(main, "_DEV_OVERRIDE", override):
            welcome = self.turn("Alex Wilber", "aad-dev", kind="welcome")
            self.assertEqual(welcome[0]["card"]["heading"], "놀이팀이시네요 👋")
            events = self.connect("Alex Wilber", "aad-dev")
            numbered = self.turn("User 9081", "aad-3", kind="welcome")  # 번호가 있는 사람은 평소대로 명단에서
        self.assertEqual(numbered[0]["card"]["heading"], "베타팀이시네요 👋")
        self.assertEqual(self.team("squad:dev-play")["status"], "connected")
        self.assertIn("squad:dev-play", self.configs())
        self.assertNotIn("squad:beta", self.configs())
        self.assertEqual(self.team("squad:beta")["status"], "new")
        with closing(sqlite3.connect(self.store)) as c:  # 개발용 명단은 명단 표에 넣지 않는다
            self.assertEqual(c.execute("SELECT scope FROM roster WHERE member_no = '9099'").fetchone()[0], "squad:beta")
        self.assertNotIn("9099", json.dumps([welcome, *events], ensure_ascii=False))

    def test_without_the_setting_the_playground_user_stays_blocked(self) -> None:
        self.roster()
        with patch.object(main, "_DEV_OVERRIDE", None):
            events = self.turn("Alex Wilber", "aad-dev", kind="welcome")
        self.assertIn("어느 팀인지 찾지 못했어요", events[0]["text"])


class LeakTests(SetupTestCase):
    def test_tokens_and_numbers_do_not_reach_cards_messages_or_logs(self) -> None:
        self.roster()
        records: list[str] = []

        class Grab(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        handler = Grab(level=logging.DEBUG)
        root = logging.getLogger()
        old = root.level
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
        try:
            events = [*self.turn("User 9071", "aad-1", kind="welcome")]
            for step in self.connect():
                events += step
            events += self.turn("User 9071", "aad-1", value={"action": "setup_github", "github_repo": "demo/repo", "github_token": "ghp_bad_FAKE00009"})
            events += self.turn("User 9071", "aad-1", value={"action": "setup_github", "github_repo": "demo/repo", "github_token": GITHUB_TOKEN})
            events += self.turn("User 1234", "aad-x", kind="welcome")
        finally:
            root.removeHandler(handler)
            root.setLevel(old)
        shown = json.dumps(events, ensure_ascii=False)
        logs = "\n".join(records)
        for secret in (NOTION_TOKEN, GITHUB_TOKEN, "ghp_bad_FAKE00009", "9071", "1234"):
            self.assertNotIn(secret, shown, secret)
            self.assertNotIn(secret, logs, secret)


class BotCardTests(unittest.TestCase):
    def test_password_input_card_is_adaptive_card_1_5(self) -> None:
        root = Path(__file__).resolve().parents[2]
        spec = importlib.util.spec_from_file_location("bot_main_for_test", root / "bot" / "main.py")
        bot = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(bot)
        card = bot._build_buttons_card({"title": "t", "buttons": [{"title": "연결하기", "data": {"action": "setup_notion"}}],
                                        "card": {"heading": "h", "text": "x", "inputs": [
                                            {"type": "text", "id": "notion_token", "label": "토큰", "style": "Password"}]}})
        data = card.model_dump(by_alias=True, exclude_none=True)
        self.assertEqual(data["version"], "1.5")
        field = next(b for b in data["body"] if b.get("id") == "notion_token")
        self.assertEqual((field["type"], field["style"]), ("Input.Text", "Password"))
        replaced = bot._build_buttons_card({"title": "h", "buttons": [], "card": {"heading": "h", "text": "입력값은 지웠어요."}})
        self.assertNotIn("Input.Text", json.dumps(replaced.model_dump(by_alias=True, exclude_none=True)))


if __name__ == "__main__":
    unittest.main()
