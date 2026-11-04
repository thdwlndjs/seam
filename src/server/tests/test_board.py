"""구조 보기·고치기 탭 테스트: 화면 데이터, 고치기·확인 답·취소, 입장 토큰, 권한. 문서: docs/v1.1/spec.md P2-8."""

from __future__ import annotations

import isolation  # noqa: F401,I001  실제 저장소·키를 쓰지 않게(맨 먼저)

import json
import sqlite3
import sys
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

import board_tab  # noqa: E402
import main  # noqa: E402
import worklog_bridge  # noqa: E402
from test_extracts import FakeExtractor  # noqa: E402
from test_parts import SAMPLE_BLOCKS, SAMPLE_MD, split_classifier  # noqa: E402
from worklog import apply, board, core, db, parts, people  # noqa: E402

SCOPE = "team:T1"
V = "test-v1"
SECRET = "test-secret"


class BoardTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.store = root / "w.sqlite3"
        core.record(self.store, scope=SCOPE, kind="notion_record", conversation_key="notion:sec-1", locator="notion:unit-1",
                    text="날짜: 2026-09-28\n" + parts.md_to_text(SAMPLE_MD), sent_at="2026-11-01T00:00:00Z")
        built = parts.build_unit_parts("unit-1", "회의록 > 1차 멘토링", SAMPLE_MD, SAMPLE_BLOCKS)
        parts.save_unit_parts(self.store, SCOPE, "notion", "unit-1", "sec-1", built, last_edited="2026-11-01T00:00:00Z")
        parts.judge_pending(self.store, SCOPE, split_classifier(), budget_tokens=None)
        parts.extract_pending(self.store, SCOPE, FakeExtractor(), V, budget_tokens=None)
        apply.request_confirm(self.store, SCOPE, "unit-1")
        apply.apply_page(self.store, SCOPE, "unit-1", "송지원", V)
        (root / "team_configs.json").write_text(json.dumps({SCOPE: {
            "notion_page_id": "root", "notion_token": "t", "github_token": "g", "github_repo": "o/r",
            "admin_aad_id": "admin"}}), encoding="utf-8")
        self.patches = [patch.object(worklog_bridge, "WORKLOG_STORE", self.store),
                        patch.object(main, "TEAM_CONFIG_PATH", str(root / "team_configs.json"))]
        for p in self.patches:
            p.start()

    def tearDown(self) -> None:
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def item(self, title: str) -> dict:
        return next(i for i in board.snapshot(self.store, SCOPE)["items"] if i["title"] == title)

    def status_of(self, item_id: str) -> str:
        with closing(sqlite3.connect(self.store)) as c:
            return c.execute("SELECT status FROM work_items WHERE id = ?", (item_id,)).fetchone()[0]


class SnapshotTests(BoardTestCase):
    def test_items_carry_source_date_quote_and_history(self) -> None:
        data = board.snapshot(self.store, SCOPE)
        task = self.item("책 등록 방법 정리")
        self.assertEqual(task["assignee"], "김팀원")
        source = task["sources"][0]
        self.assertEqual((source["title"], source["date"]), ("1차 멘토링", "2026-09-28"))  # 날짜 속성이 마지막 수정일보다 먼저
        self.assertEqual(source["url"], "https://www.notion.so/unit1")
        self.assertIn("책 등록은 어떻게?", task["quotes"])
        self.assertEqual(task["history"][0]["actor"], "송지원")
        self.assertEqual([s["title"] for s in data["sources"]], ["1차 멘토링"])
        self.assertEqual(data["recent_notes"], 1)

    def test_date_falls_back_to_last_edited(self) -> None:
        self.assertEqual(board._page_date("제목: 회의", "2026-10-02T01:00:00Z"), "2026-10-02")
        self.assertEqual(board._page_date("일자: 2026-09-29", None), "2026-09-29")


class EditTests(BoardTestCase):
    def test_edit_records_a_revision_and_skips_no_change(self) -> None:
        task = self.item("책 등록 방법 정리")
        self.assertFalse(board.edit(self.store, SCOPE, task["id"], {"title": "책 등록 방법 정리"}, "송지원")["changed"])
        board.edit(self.store, SCOPE, task["id"], {"title": "책 등록 흐름 정리", "assignee": "박디자"}, "송지원")
        edited = self.item("책 등록 흐름 정리")
        self.assertEqual(edited["assignee"], "박디자")
        self.assertEqual(edited["history"][-1]["reason"], board.EDIT_REASON)

    def test_kind_change_fixes_status_and_blocks_duplicates(self) -> None:
        task = self.item("책 등록 방법 정리")
        board.edit(self.store, SCOPE, task["id"], {"kind": "decision"}, "송지원")
        self.assertEqual(self.status_of(task["id"]), "active")
        opened = self.item("AI를 어디에 쓸지")
        with self.assertRaises(db.ConflictError):
            board.edit(self.store, SCOPE, opened["id"], {"kind": "decision", "title": "책 등록 방법 정리"}, "송지원")
        with self.assertRaises(db.ValidationError):
            board.edit(self.store, SCOPE, opened["id"], {"title": "  "}, "송지원")

    def test_answers_and_cancel(self) -> None:
        opened = self.item("AI를 어디에 쓸지")
        board.resolve(self.store, SCOPE, opened["id"], "confirm", "송지원")
        self.assertEqual(self.item("AI를 어디에 쓸지")["kind"], "decision")  # 미정을 확정하면 결정
        board.resolve(self.store, SCOPE, opened["id"], "open", "송지원")
        self.assertEqual(self.item("AI를 어디에 쓸지")["kind"], "open")
        board.resolve(self.store, SCOPE, opened["id"], "ignore", "송지원")
        self.assertEqual(self.status_of(opened["id"]), "cancelled")
        self.assertNotIn("AI를 어디에 쓸지", {i["title"] for i in board.snapshot(self.store, SCOPE)["items"]})
        with self.assertRaises(db.ConflictError):
            board.cancel(self.store, SCOPE, opened["id"], "송지원")
        with self.assertRaises(db.NotFoundError):
            board.edit(self.store, "team:other", self.item("책 등록 방법 정리")["id"], {"title": "x"}, "송지원")


class TokenTests(unittest.TestCase):
    def test_token_round_trip_tamper_and_expiry(self) -> None:
        token = board_tab.issue(SECRET, "u1", "김팀원", SCOPE, now=1000)
        self.assertEqual(board_tab.verify(SECRET, token, now=1001)["s"], SCOPE)
        for bad in (token + "x", board_tab.issue("other", "u1", "김팀원", SCOPE, now=1000), "", None):
            with self.assertRaises(board_tab.HTTPException):
                board_tab.verify(SECRET, bad, now=1001)
        with self.assertRaises(board_tab.HTTPException):
            board_tab.verify(SECRET, token, now=1000 + board_tab.TOKEN_HOURS * 3600 + 1)

    def test_link_uses_teams_deep_link_when_app_id_is_set(self) -> None:
        with patch.dict("os.environ", {"BOARD_PUBLIC_URL": "https://bot.example.com/"}, clear=False):
            self.assertTrue(board_tab.link(SECRET, "u1", "김", SCOPE).startswith("https://bot.example.com/tab/board?t="))
        with patch.dict("os.environ", {"TEAMS_APP_ID": "app-1"}, clear=False):
            url = board_tab.link(SECRET, "u1", "김", SCOPE)
        self.assertTrue(url.startswith("https://teams.microsoft.com/l/entity/app-1/board?context="))


class ApiTests(BoardTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.client = TestClient(main.app)

    def auth(self, user: str, name: str = "김팀원") -> dict:
        return {"Authorization": "Bearer " + board_tab.issue(SECRET, user, name, SCOPE)}

    def test_page_and_data_need_a_valid_link(self) -> None:
        self.assertEqual(self.client.get("/tab/board").status_code, 200)
        self.assertEqual(self.client.get("/api/board").status_code, 401)
        data = self.client.get("/api/board", headers=self.auth("u1")).json()
        self.assertFalse(data["viewer"]["can_edit"])
        self.assertTrue(self.client.get("/api/board", headers=self.auth("admin")).json()["viewer"]["can_edit"])

    def test_only_approvers_edit(self) -> None:
        task = self.item("책 등록 방법 정리")
        path = f"/api/board/items/{task['id']}"
        self.assertEqual(self.client.post(path, json={"title": "새 제목"}, headers=self.auth("u1")).status_code, 403)
        people.touch(self.store, SCOPE, "dev1", "개발자")
        people.set_role(self.store, "dev1", "dev")
        resp = self.client.post(path, json={"title": "새 제목"}, headers=self.auth("dev1", "개발자"))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.item("새 제목")["history"][-1]["actor"], "개발자")
        clash = self.client.post(f"/api/board/items/{task['id']}/answer", json={"answer": "maybe"}, headers=self.auth("admin"))
        self.assertEqual(clash.status_code, 409)

    def test_menu_card_has_the_board_link(self) -> None:
        turn = {"kind": "welcome", "user": {"aad_id": "u1", "name": "김팀원"}, "team": {"id": "T1", "name": "알파"},
                "text": "", "value": None, "conversation": {"id": "C1", "type": "channel"},
                "message_id": "m", "sent_at": "2026-11-04T01:00:00Z"}
        with patch.object(main, "_check_on_open", lambda *a, **k: None):
            body = self.client.post("/api/turn", json=turn, headers={"X-Bot-Secret": SECRET}).text
        events = [json.loads(line) for line in body.splitlines() if line.strip()]
        urls = [b.get("url") for e in events if e["type"] == "buttons" for b in e["buttons"]]
        self.assertTrue(any(u and "/tab/board?t=" in u for u in urls), events)


if __name__ == "__main__":
    unittest.main()
