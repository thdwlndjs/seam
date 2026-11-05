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


class KnowledgeBoardTests(BoardTestCase):
    """v2.1 Phase 3: 산출물 문서·관계·성숙도·삭제/복원·서버 검색·사람 수정 보호."""

    def setUp(self) -> None:
        super().setUp()
        self.client = TestClient(main.app)
        base = self.item("AI를 어디에 쓸지")["id"]
        with db.write(self.store) as c:
            ts = db.now()
            self.doc_id = core._create_item(c, SCOPE, "artifact", {
                "title": "로그인 기획서", "statement": "# 요약\n- 이메일 로그인을 지원한다\n\n원문: https://www.notion.so/doc1",
                "artifact_type": "기획", "maturity": "draft"}, "문서", "자동(산출물 해석)", ts)
            self.req_id = core._create_item(c, SCOPE, "requirement", {
                "title": "이메일 로그인 필수", "statement": "이메일로 로그인할 수 있어야 한다", "maturity": "draft",
                "github_refs": [{"type": "issue", "number": 7, "basis": "inferred"}]}, "문서", "송지원", ts)
            self.alt_id = core._create_item(c, SCOPE, "decision", {
                "title": "소셜 로그인 안", "statement": "소셜 로그인으로 하자는 가안", "maturity": "draft"}, "문서", "송지원", ts)
            core._link(c, self.req_id, self.doc_id, core.LINK_DERIVED_FROM, None, ts)
            core._link(c, self.alt_id, base, core.LINK_ALTERNATIVE_TO, None, ts)
            self.base_id = base

    def auth(self, user: str = "admin", name: str = "관리자") -> dict:
        return {"Authorization": "Bearer " + board_tab.issue(SECRET, user, name, SCOPE)}

    def test_snapshot_has_documents_relations_maturity_and_github(self) -> None:
        data = board.snapshot(self.store, SCOPE)
        doc = next(a for a in data["artifacts"] if a["id"] == self.doc_id)
        self.assertEqual((doc["artifact_type"], doc["maturity"]), ("기획", "draft"))
        self.assertEqual([d["id"] for d in doc["derived"]], [self.req_id])  # 이 문서에서 나온 항목
        req = self.item("이메일 로그인 필수")
        self.assertEqual(req["maturity"], "draft")
        self.assertEqual(req["github"], [{"type": "issue", "number": 7, "basis": "inferred"}])  # 추정 연결은 추정으로
        self.assertEqual([(link["type"], link["direction"]) for link in req["links"]], [("DERIVED_FROM", "out")])
        alt = self.item("소셜 로그인 안")
        base = self.item("AI를 어디에 쓸지")
        self.assertEqual(alt["links"][0]["other"]["id"], base["id"])
        self.assertEqual(base["links"][0]["direction"], "in")  # 기존 결정 쪽에서는 "대안이 있음"으로 보인다
        self.assertNotIn(self.doc_id, [i["id"] for i in data["items"]])  # 문서는 항목 목록과 따로

    def test_conflict_flag_and_human_edit_marker(self) -> None:
        with db.write(self.store) as c:
            core._link(c, self.alt_id, self.req_id, core.LINK_RELATED_TO, None, db.now())
        self.assertTrue(self.item("소셜 로그인 안")["conflict"])
        self.assertFalse(self.item("이메일 로그인 필수")["human_edited"])
        board.edit(self.store, SCOPE, self.req_id, {"statement": "이메일 또는 소셜로 로그인한다"}, "송지원")
        edited = self.item("이메일 로그인 필수")
        self.assertTrue(edited["human_edited"])
        self.assertTrue(edited["history"][-1]["by_human"])
        self.assertFalse(edited["history"][0]["by_human"])

    def test_editing_a_document_summary_reaches_search_immediately(self) -> None:
        self.assertEqual(self.client.get("/api/board/search", params={"q": "푸시알림"}, headers=self.auth()).json()["ids"], [])
        board.edit(self.store, SCOPE, self.doc_id, {"statement": "# 요약\n- 푸시알림 동의를 받는다"}, "송지원")
        ids = self.client.get("/api/board/search", params={"q": "푸시알림"}, headers=self.auth()).json()["ids"]
        self.assertEqual(ids, [self.doc_id])
        with self.assertRaises(db.ValidationError):
            board.edit(self.store, SCOPE, self.doc_id, {"kind": "decision"}, "송지원")  # 문서는 종류를 바꾸지 못한다
        board.edit(self.store, SCOPE, self.alt_id, {"maturity": "agreed"}, "송지원")
        self.assertEqual(self.item("소셜 로그인 안")["maturity"], "agreed")
        with self.assertRaises(db.ValidationError):
            board.edit(self.store, SCOPE, self.alt_id, {"maturity": "최종"}, "송지원")

    def test_delete_hides_from_search_and_restore_brings_it_back_with_history(self) -> None:
        def hits() -> list[str]:
            return self.client.get("/api/board/search", params={"q": "소셜 로그인"}, headers=self.auth()).json()["ids"]

        self.assertIn(self.alt_id, hits())
        self.client.post(f"/api/board/items/{self.alt_id}/cancel", headers=self.auth())
        self.assertNotIn(self.alt_id, hits())  # 검색·리딩·질문에서 빠진다
        data = board.snapshot(self.store, SCOPE)
        self.assertNotIn(self.alt_id, [i["id"] for i in data["items"]])
        self.assertIn(self.alt_id, [i["id"] for i in data["deleted"]])
        self.assertEqual(self.client.post(f"/api/board/items/{self.alt_id}/restore", headers=self.auth("u1", "팀원")).status_code, 403)
        self.assertEqual(self.client.post(f"/api/board/items/{self.alt_id}/restore", headers=self.auth()).status_code, 200)
        self.assertIn(self.alt_id, hits())
        self.assertEqual(self.status_of(self.alt_id), "active")
        reasons = [h["reason"] for h in self.item("소셜 로그인 안")["history"]]
        self.assertEqual(reasons[-2:], ["구조 화면에서 반영 취소", board.RESTORE_REASON])  # 이력에 둘 다 남는다
        self.assertEqual(board.snapshot(self.store, SCOPE)["deleted"], [])
        again = self.client.post(f"/api/board/items/{self.alt_id}/restore", headers=self.auth())
        self.assertEqual(again.status_code, 409)  # 삭제 상태가 아니면 복원하지 않는다

    def test_restore_is_blocked_when_a_live_item_has_the_same_title(self) -> None:
        board.cancel(self.store, SCOPE, self.alt_id, "송지원")
        with db.write(self.store) as c:
            core._create_item(c, SCOPE, "decision", {"title": "소셜 로그인 안", "statement": "새로 적은 같은 제목"}, "새", "송지원", db.now())
        with self.assertRaises(db.ConflictError):
            board.restore(self.store, SCOPE, self.alt_id, "송지원")

    def test_search_needs_a_valid_link_and_stays_inside_the_team(self) -> None:
        self.assertEqual(self.client.get("/api/board/search", params={"q": "로그인"}).status_code, 401)
        other = {"Authorization": "Bearer " + board_tab.issue(SECRET, "admin", "관리자", "team:other")}
        self.assertEqual(self.client.get("/api/board/search", params={"q": "로그인"}, headers=other).json()["ids"], [])

    def test_a_recollection_does_not_overwrite_a_human_edit_and_asks_instead(self) -> None:
        board.edit(self.store, SCOPE, self.base_id, {"statement": "사람이 고친 설명"}, "송지원")
        page = parts.build_unit_parts("unit-2", "회의록 > 2차", "# 2차\n- 센서 가격을 다시 본다\n", None)
        core.record(self.store, scope=SCOPE, kind="notion_record", conversation_key="notion:sec-1", locator="notion:unit-2",
                    text="센서 가격을 다시 본다", sent_at="2026-11-02T00:00:00Z")
        parts.save_unit_parts(self.store, SCOPE, "notion", "unit-2", "sec-1", page, last_edited="2026-11-02T00:00:00Z")
        for part in parts.list_parts(self.store, SCOPE, page_id="unit-2"):
            parts.set_part_kind(self.store, part["id"], "meeting", "송지원")

        def extractor(payload: dict) -> dict:
            return {"items": [{"kind": "open", "title": "AI를 어디에 쓸지 다시", "quote": "센서 가격을 다시 본다"}], "usage": {}, "model": "fake"}

        parts.extract_pending(self.store, SCOPE, extractor, V, budget_tokens=None)

        def judge(item: dict, related: list[dict]) -> dict:
            return {"change_type": "supplementary", "target_id": self.base_id, "affected_task_ids": [], "rationale": "보충"}

        apply.apply_page(self.store, SCOPE, "unit-2", "송지원", V, judge=judge)
        statement = next(i for i in board.snapshot(self.store, SCOPE)["items"] if i["id"] == self.base_id)["statement"]
        self.assertEqual(statement, "사람이 고친 설명")  # 덮어쓰지 않았다
        self.assertEqual(self.item("AI를 어디에 쓸지 다시")["status"], "needs_discussion")  # 대신 확인 필요로 보냈다

    def test_the_board_never_writes_to_notion_or_github(self) -> None:
        import socket

        attempts: list[object] = []
        real_connect = socket.socket.connect

        def guarded(sock, address, *args, **kwargs):
            host = address[0] if isinstance(address, tuple) else address
            if host not in ("127.0.0.1", "::1", "localhost"):  # asyncio 내부의 루프백 연결은 허용, 바깥 연결만 막는다
                attempts.append(address)
                raise OSError("network is blocked in this test")
            return real_connect(sock, address, *args, **kwargs)

        task = self.item("책 등록 방법 정리")
        with patch.object(socket.socket, "connect", guarded):
            headers = self.auth()
            self.assertEqual(self.client.get("/api/board", headers=headers).status_code, 200)
            self.assertEqual(self.client.get("/api/board/search", params={"q": "로그인"}, headers=headers).status_code, 200)
            self.assertEqual(self.client.post(f"/api/board/items/{task['id']}", json={"title": "새 이름", "maturity": "agreed"}, headers=headers).status_code, 200)
            self.assertEqual(self.client.post(f"/api/board/items/{task['id']}/cancel", headers=headers).status_code, 200)
            self.assertEqual(self.client.post(f"/api/board/items/{task['id']}/restore", headers=headers).status_code, 200)
            opened = self.item("AI를 어디에 쓸지")
            self.assertEqual(self.client.post(f"/api/board/items/{opened['id']}/answer", json={"answer": "open"}, headers=headers).status_code, 200)
        self.assertEqual(attempts, [])  # 바깥으로 나가는 연결이 하나도 없었다


class TabPageSafetyTests(unittest.TestCase):
    def test_the_page_renders_user_text_without_inserting_html(self) -> None:
        source = (Path(__file__).resolve().parents[1] / "tab" / "board.html").read_text(encoding="utf-8")
        script = source.split("<script>", 2)[-1]
        # 문서 요약·제목·인용 같은 저장된 글은 innerHTML이 아니라 DOM 노드/textContent로만 들어간다(아이콘 상수만 html로 넣는다)
        for use in script.split("html: ")[1:]:
            self.assertTrue(use.startswith(("ICON.", "icon ")), use[:40])  # 아이콘 상수(또는 그것을 넘겨받은 변수)만
        self.assertNotIn("innerHTML =", script.replace("if (k === \"html\") el.innerHTML = v;", ""))
        self.assertIn("(https?:", script)  # 링크는 http(s)만


if __name__ == "__main__":
    unittest.main()
