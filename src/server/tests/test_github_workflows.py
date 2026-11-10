from __future__ import annotations

import isolation  # noqa: F401
import asyncio
import json
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import main
import httpx
import importlib.util
import worklog_bridge
from worklog import board, core, db, github


SCOPE = "team:github-workflows"


class GitHubKnowledgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Path(self.tmp.name) / "copy.sqlite3"
        db.initialize(self.store)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _make_task(self, title: str, refs: list[dict], status: str = "open") -> str:
        with db.write(self.store) as c:
            return core._create_item(c, SCOPE, "task", {
                "title": title, "statement": "기존 할 일 설명", "status": status, "github_refs": refs,
            }, "테스트 준비", "테스터", db.now())

    def test_issue_breakdown_accepts_only_known_team_assignees(self) -> None:
        raw = {"issues": [{"title": "로그인 API", "body": "API 요청과 응답을 구현한다.",
                           "acceptance_criteria": ["성공 응답을 반환한다"], "assignee": "dev-login"}]}
        result = github.validate_issue_proposals(raw, {"dev-login": "개발자"})
        self.assertEqual(result, [{"title": "로그인 API", "body": "API 요청과 응답을 구현한다.",
                                   "acceptance_criteria": ["성공 응답을 반환한다"],
                                   "assignee": "dev-login", "assignee_name": "개발자", "include": True}])
        with self.assertRaises(db.ContractError):
            github.validate_issue_proposals(raw, {"other-login": "다른 사람"})

    def test_work_share_requires_exact_fields_and_rejects_unverified_claims(self) -> None:
        self.assertEqual(github.validate_work_share({"title": "로그인 PR", "summary": "로그인 화면 변경을 공유합니다."}),
                         {"title": "로그인 PR", "summary": "로그인 화면 변경을 공유합니다."})
        with self.assertRaises(db.ContractError):
            github.validate_work_share({"title": "완료", "summary": "테스트 통과", "url": "https://example.test"})
        with self.assertRaises(db.ContractError):
            github.validate_work_share({"title": "완료", "summary": "동작 확인 완료"})

    def test_created_github_issue_is_saved_as_an_explicitly_linked_task(self) -> None:
        with db.write(self.store) as c:
            source_id = core._create_item(c, SCOPE, "decision", {
                "title": "OAuth 로그인", "statement": "OAuth 로그인을 구현하기로 함", "maturity": "agreed",
            }, "테스트 준비", "테스터", db.now())
        item_id = github.record_created_task(
            self.store, SCOPE,
            {"title": "OAuth 로그인", "body": "로그인 흐름을 추가한다.",
             "acceptance_criteria": ["OAuth 성공 시 세션이 생성된다"], "assignee_name": "개발자"},
            {"number": 42, "url": "https://github.com/example/app/issues/42"}, "승인자", source_id)
        with closing(db.connect(self.store)) as c:
            row = c.execute("SELECT * FROM work_items WHERE id=?", (item_id,)).fetchone()
        self.assertEqual(row["kind"], "task")
        self.assertEqual(row["status"], "open")
        attrs = json.loads(row["attributes_json"])
        self.assertEqual(attrs["assignee"], "개발자")
        self.assertEqual(attrs["github_refs"], [{"type": "issue", "number": 42, "basis": "explicit"}])
        self.assertIn("https://github.com/example/app/issues/42", row["statement_md"])
        with closing(db.connect(self.store)) as c:
            self.assertIsNotNone(c.execute("SELECT 1 FROM work_links WHERE source_id=? AND target_id=? AND type='AFFECTS'",
                                           (source_id, item_id)).fetchone())

    def test_completion_updates_only_explicitly_linked_tasks_and_records_history(self) -> None:
        explicit = self._make_task("명시 연결", [{"type": "issue", "number": 42, "basis": "explicit"}])
        inferred = self._make_task("추정 연결", [{"type": "issue", "number": 42, "basis": "inferred"}])
        result = github.complete_linked_tasks(self.store, SCOPE, {42}, "example/app", "승인자")
        self.assertEqual([r["id"] for r in result], [explicit])
        with closing(db.connect(self.store)) as c:
            rows = {r["id"]: r for r in c.execute("SELECT * FROM work_items WHERE id IN (?,?)", (explicit, inferred))}
            revisions = c.execute("SELECT COUNT(*) FROM revisions WHERE object_id=?", (explicit,)).fetchone()[0]
        self.assertEqual(rows[explicit]["status"], "done")
        self.assertEqual(rows[inferred]["status"], "open")
        self.assertIn("검증 여부는 확인되지 않음", rows[explicit]["statement_md"])
        self.assertEqual(revisions, 2)
        self.assertEqual(github.completed_tasks(self.store, SCOPE),
                         [{"title": "명시 연결", "assignee": None, "numbers": [42]}])
        self.assertEqual(github.complete_linked_tasks(self.store, SCOPE, {42}, "example/app", "승인자"), [])

    def test_shared_development_artifact_keeps_github_url_as_source(self) -> None:
        item_id = github.record_shared_artifact(self.store, SCOPE, "결제 API PR", "결제 API 변경 요약",
                                                "https://github.com/example/app/pull/9", "pr", "공유자")
        with closing(db.connect(self.store)) as c:
            row = c.execute("SELECT * FROM work_items WHERE id=?", (item_id,)).fetchone()
            source = c.execute("SELECT * FROM sources WHERE locator=?", ("https://github.com/example/app/pull/9",)).fetchone()
            evidence = c.execute("SELECT * FROM object_evidence WHERE object_id=?", (item_id,)).fetchone()
        self.assertEqual(row["kind"], "artifact")
        self.assertIn("https://github.com/example/app/pull/9", row["statement_md"])
        self.assertIsNotNone(source)
        self.assertEqual(json.loads(source["metadata_json"])["github_type"], "pr")
        self.assertIsNotNone(evidence)
        saved = next(item for item in board.snapshot(self.store, SCOPE)["artifacts"] if item["id"] == item_id)
        self.assertEqual(saved["sources"][0]["url"], "https://github.com/example/app/pull/9")
        self.assertEqual(saved["sources"][0]["origin"], "github")


class GitHubTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_post_sends_json_to_repository_api_and_returns_created_issue(self) -> None:
        response = SimpleNamespace(json=lambda: {"number": 42, "html_url": "https://github.com/example/app/issues/42"},
                                   raise_for_status=lambda: None)
        client = SimpleNamespace(post=AsyncMock(return_value=response))
        payload = {"title": "로그인 API", "body": "요구사항에 따른 작업", "assignees": ["dev-login"]}
        with patch.object(main, "_get_github_http_client", return_value=client):
            result = await main._github_post("example/app", "/issues", "fake-token", payload)
        self.assertEqual(result["number"], 42)
        args, kwargs = client.post.await_args
        self.assertEqual(args[0], "https://api.github.com/repos/example/app/issues")
        self.assertEqual(kwargs["json"], payload)
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer fake-token")


class GitHubIssueApprovalTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Path(self.tmp.name) / "copy.sqlite3"
        db.initialize(self.store)
        main.github_issue_sessions.clear()

    def tearDown(self) -> None:
        main.github_issue_sessions.clear()
        self.tmp.cleanup()

    def _turn(self):
        return main.Turn(main.TurnRequest(
            user={"aad_id": "reviewer", "name": "승인자"}, team={"id": "github-team", "name": "팀"},
            text="", value=None, conversation={"id": "C1", "type": "channel"}, message_id="m", sent_at=None))

    def _session(self, issue_count: int = 1):
        with db.write(self.store) as c:
            source_id = core._create_item(c, "team:github-team", "decision", {
                "title": "로그인 방식", "statement": "OAuth 방식으로 구현하기로 함", "maturity": "agreed",
            }, "테스트 준비", "테스터", db.now())
        issues = [{"title": f"이슈 {n}", "body": "작업 본문", "acceptance_criteria": ["조건을 만족한다"],
                   "assignee": "dev", "assignee_name": "개발자"} for n in range(issue_count)]
        session_id = "review-session"
        main.github_issue_sessions["reviewer"] = {
            "session_id": session_id, "team_key": "team:github-team", "user_id": "reviewer",
            "source_item_id": source_id, "repo": "example/app", "issues": issues,
            "assignees": {"dev": "개발자"}, "mode": "preview", "at": main.time.monotonic(),
        }
        value = {"session_id": session_id}
        for n in range(issue_count):
            value.update({f"issue_{n}_include": "true", f"issue_{n}_title": f"이슈 {n}",
                          f"issue_{n}_body": "작업 본문", f"issue_{n}_criteria": "조건을 만족한다",
                          f"issue_{n}_assignee": "dev"})
        return source_id, value

    async def test_only_confirmed_issue_is_linked_to_saved_task(self) -> None:
        source_id, value = self._session()
        response = {"number": 42, "html_url": "https://github.com/example/app/issues/42"}
        post = AsyncMock(return_value=response)
        with patch.object(worklog_bridge, "WORKLOG_STORE", self.store), \
             patch.object(main, "_get_config", return_value={"github_repo": "example/app", "github_token": "fake"}), \
             patch.object(main, "_is_approver", return_value=True), \
             patch.object(main, "_github_post", post):
            await main._handle_issue_proposal_action(self._turn(), "issue_register", value)
        post.assert_awaited_once()
        with closing(db.connect(self.store)) as c:
            task = c.execute("SELECT * FROM work_items WHERE kind='task'").fetchone()
            link = c.execute("SELECT 1 FROM work_links WHERE source_id=? AND target_id=? AND type='AFFECTS'",
                             (source_id, task["id"])).fetchone()
        self.assertEqual(json.loads(task["attributes_json"])["github_refs"][0]["number"], 42)
        self.assertIsNotNone(link)
        self.assertNotIn("reviewer", main.github_issue_sessions)

    async def test_unapproved_submit_never_calls_github(self) -> None:
        _, value = self._session()
        post = AsyncMock()
        with patch.object(worklog_bridge, "WORKLOG_STORE", self.store), \
             patch.object(main, "_get_config", return_value={"github_repo": "example/app", "github_token": "fake"}), \
             patch.object(main, "_is_approver", return_value=False), \
             patch.object(main, "_github_post", post):
            await main._handle_issue_proposal_action(self._turn(), "issue_register", value)
        post.assert_not_awaited()
        with closing(db.connect(self.store)) as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM work_items WHERE kind='task'").fetchone()[0], 0)

    async def test_duplicate_submit_during_issue_creation_creates_only_once(self) -> None:
        _, value = self._session()
        started = asyncio.Event()
        release = asyncio.Event()
        calls = 0
        async def post(*args):
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()
            return {"number": 42, "html_url": "https://github.com/example/app/issues/42"}
        with patch.object(worklog_bridge, "WORKLOG_STORE", self.store), \
             patch.object(main, "_get_config", return_value={"github_repo": "example/app", "github_token": "fake"}), \
             patch.object(main, "_is_approver", return_value=True), \
             patch.object(main, "_github_post", new=post):
            first = asyncio.create_task(main._handle_issue_proposal_action(self._turn(), "issue_register", value))
            await started.wait()
            await main._handle_issue_proposal_action(self._turn(), "issue_register", value)
            release.set()
            await first
        self.assertEqual(calls, 1)
        with closing(db.connect(self.store)) as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM work_items WHERE kind='task'").fetchone()[0], 1)

    async def test_partial_failure_records_only_created_issue_and_reports_failure(self) -> None:
        _, value = self._session(issue_count=2)
        denied = httpx.HTTPStatusError("forbidden", request=httpx.Request("POST", "https://api.github.com"),
                                       response=httpx.Response(403, request=httpx.Request("POST", "https://api.github.com")))
        post = AsyncMock(side_effect=[{"number": 42, "html_url": "https://github.com/example/app/issues/42"}, denied])
        turn = self._turn()
        with patch.object(worklog_bridge, "WORKLOG_STORE", self.store), \
             patch.object(main, "_get_config", return_value={"github_repo": "example/app", "github_token": "fake"}), \
             patch.object(main, "_is_approver", return_value=True), \
             patch.object(main, "_github_post", post):
            await main._handle_issue_proposal_action(turn, "issue_register", value)
        with closing(db.connect(self.store)) as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM work_items WHERE kind='task'").fetchone()[0], 1)
        events = []
        while not turn.queue.empty():
            event = turn.queue.get_nowait()
            if event:
                events.append(event)
        result_cards = [event for event in events if event.get("type") == "buttons"]
        self.assertTrue(any("1건 생성, 1건 실패" in event["title"] and "HTTP 403" in event["title"]
                            for event in result_cards))
        self.assertFalse(any(button.get("data", {}).get("action") == "issue_register"
                             for event in result_cards for button in event.get("buttons", [])))


class GitHubWorkShareTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Path(self.tmp.name) / "copy.sqlite3"
        db.initialize(self.store)
        main.github_share_sessions.clear()

    def tearDown(self) -> None:
        main.github_share_sessions.clear()
        self.tmp.cleanup()

    def _turn(self):
        return main.Turn(main.TurnRequest(
            user={"aad_id": "reviewer", "name": "검토자"}, team={"id": "github-team", "name": "팀"},
            text="", value=None, conversation={"id": "C1", "type": "channel"}, message_id="m", sent_at=None))

    def _session(self):
        session_id = "share-session"
        key = "pr:9"
        main.github_share_sessions["reviewer"] = {
            "session_id": session_id, "team_key": "team:github-team", "user_id": "reviewer",
            "repo": "example/app", "entries": {key: {"type": "pr", "number": 9, "title": "Fix #7"}},
            "mode": "select", "at": main.time.monotonic(),
        }
        return {"session_id": session_id, "item_key": key}

    def _completed_task(self, complete: bool = True):
        with db.write(self.store) as c:
            item_id = core._create_item(c, "team:github-team", "task", {
                "title": "로그인 API 작업", "statement": "로그인 API를 구현", "assignee": "검토자",
                "github_refs": [{"type": "issue", "number": 42, "basis": "explicit"}],
            }, "테스트 준비", "테스터", db.now())
        if complete:
            github.complete_linked_tasks(self.store, "team:github-team", {42}, "example/app", "승인자")
        return item_id

    async def test_opening_list_only_reads_recent_prs_and_commits(self) -> None:
        turn = self._turn()
        with patch.object(main, "_github_prs", new=AsyncMock(return_value=[{"number": 9, "title": "PR"}])), \
             patch.object(main, "_github_commits", new=AsyncMock(return_value=[{"sha": "abcd1234", "title": "commit"}])), \
             patch.object(worklog_bridge, "lead_call", side_effect=AssertionError("listing must not call LLM")):
            await main._show_work_share_list(turn, {"github_repo": "example/app", "github_token": "fake"})
        event = turn.queue.get_nowait()
        self.assertEqual(event["type"], "buttons")
        self.assertEqual(len(event["buttons"]), 2)
        self.assertEqual(main.github_share_sessions["reviewer"]["mode"], "select")

    async def test_preview_uses_one_llm_call_and_only_publish_records_artifact(self) -> None:
        value = self._session()
        turn = self._turn()
        def get(repo, path, token, params=None):
            if path == "/pulls/9":
                return {"title": "Fix login #7", "body": "Login form update"}
            if path == "/pulls/9/files":
                return [{"filename": "src/login.py"}]
            if path == "/issues/7":
                return {"title": "Login behavior", "body": "Existing issue", "state": "open"}
            raise AssertionError(path)
        llm = MagicMock(return_value=({"title": "로그인 화면 PR", "summary": "로그인 화면을 바꿨어요."},
                                     {"prompt_tokens": 12, "completion_tokens": 4}, "fake"))
        config = {"github_repo": "example/app", "github_token": "fake"}
        with patch.object(worklog_bridge, "WORKLOG_STORE", self.store), \
             patch.object(main, "_get_config", return_value=config), \
             patch.object(main, "_github_get_cached", new=AsyncMock(side_effect=get)), \
             patch.object(main, "_github_pr_files", new=AsyncMock(return_value=["src/login.py"])), \
             patch.object(worklog_bridge, "lead_call", llm):
            await main._handle_work_share_action(turn, "work_share_select", value)
        llm.assert_called_once()
        with closing(db.connect(self.store)) as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM work_items WHERE kind='artifact'").fetchone()[0], 0)
        session = main.github_share_sessions["reviewer"]
        self.assertEqual(session["mode"], "preview")
        self.assertIn("검증 여부: 확인하지 않았어요", session["preview_text"])
        publish = {"session_id": value["session_id"]}
        with patch.object(worklog_bridge, "WORKLOG_STORE", self.store), patch.object(main, "_get_config", return_value=config):
            await main._handle_work_share_action(turn, "work_share_publish", publish)
        with closing(db.connect(self.store)) as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM work_items WHERE kind='artifact'").fetchone()[0], 1)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM sources WHERE kind='github'").fetchone()[0], 1)
        llm.assert_called_once()
        self.assertNotIn("reviewer", main.github_share_sessions)

    async def test_cancel_does_not_save_or_publish(self) -> None:
        value = self._session()
        turn = self._turn()
        with patch.object(worklog_bridge, "WORKLOG_STORE", self.store), \
             patch.object(main, "_get_config", return_value={"github_repo": "example/app", "github_token": "fake"}), \
             patch.object(worklog_bridge, "lead_call", side_effect=AssertionError("cancel must not call LLM")):
            await main._handle_work_share_action(turn, "work_share_cancel", value)
        with closing(db.connect(self.store)) as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM work_items WHERE kind='artifact'").fetchone()[0], 0)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM sources WHERE kind='github'").fetchone()[0], 0)

    async def test_menu_entry_uses_api_lists_without_github_identity_prompt(self) -> None:
        turn = self._turn()
        with patch.object(main, "_get_config", return_value={"github_repo": "example/app", "github_token": "fake"}), \
             patch.object(main, "_show_work_share_list", new=AsyncMock()) as show_list:
            await main._run_menu(turn, "4-2")
        show_list.assert_awaited_once()

    async def test_completed_work_updates_linked_saved_task_after_project_success(self) -> None:
        item_id = self._completed_task(complete=False)
        async def get(repo, path, token, params=None):
            if path == "/issues":
                return [{"number": 42, "title": "로그인 API 작업", "state": "closed", "assignees": [{"login": "reviewer"}]}]
            if path == "/commits":
                return [{"commit": {"message": "Fix #42"}}]
            raise AssertionError(path)
        with patch.object(worklog_bridge, "WORKLOG_STORE", self.store), \
             patch.object(main, "_github_get_cached", new=AsyncMock(side_effect=get)), \
             patch.object(main, "_find_project_status_field", new=AsyncMock(return_value=("p", "f", "done"))), \
             patch.object(main, "_find_project_item_id", new=AsyncMock(return_value="item")), \
             patch.object(main, "_set_project_item_status", new=AsyncMock()) as update:
            text = await main.share_completed_work({"github_repo": "example/app", "github_token": "fake",
                                                    "github_project_number": 4}, "reviewer", "team:github-team", "승인자")
        update.assert_awaited_once()
        self.assertIn("저장된 할 일 1건을 GitHub 완료", text)
        with closing(db.connect(self.store)) as c:
            task = c.execute("SELECT status, statement_md FROM work_items WHERE id=?", (item_id,)).fetchone()
        self.assertEqual(task["status"], "done")
        self.assertIn("동작 검증 여부는 확인되지 않음", task["statement_md"])

    async def test_menu_2_and_menu_3_label_saved_completion_as_github_completion(self) -> None:
        self._completed_task()
        config = {"github_repo": "example/app", "github_token": "fake"}
        with patch.object(worklog_bridge, "WORKLOG_STORE", self.store), \
             patch.object(main, "_stored_notes", new=AsyncMock(return_value={"texts": ["팀 기록"], "as_of": None})), \
             patch.object(main, "_note_footer", return_value=[]), \
             patch.object(main, "_stored_items", return_value={"confirmed": [], "undecided": [], "discussion": [],
                   "decisions": [], "feedback": [], "pending": 0, "to_confirm": 0, "board": {"candidate": None}}), \
             patch.object(main, "_github_get_cached", new=AsyncMock(return_value=[])):
            team_text, _ = await main.fetch_team_status(config, "team:github-team")
            role_text = await main.fetch_my_role("reviewer", "검토자", config, "team:github-team")
        self.assertIn("GitHub 완료 (동작 검증은 확인하지 않음)", team_text)
        self.assertIn("로그인 API 작업", team_text)
        self.assertIn("GitHub 완료 (동작 검증은 확인하지 않음)", role_text)
        self.assertIn("로그인 API 작업", role_text)


class AdaptiveCardFormTests(unittest.TestCase):
    def test_issue_review_form_renders_editable_fields_and_actions(self) -> None:
        path = Path(__file__).resolve().parents[2] / "bot" / "main.py"
        spec = importlib.util.spec_from_file_location("phase7_bot_main", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        event = {"title": "Review", "buttons": [
            {"title": "등록", "data": {"action": "issue_register"}},
            {"title": "취소", "data": {"action": "issue_cancel"}},
        ], "card": {"heading": "검토", "text": "선택 후 등록", "inputs": [
            {"type": "toggle", "id": "issue_0_include", "label": "등록", "value": "true"},
            {"type": "text", "id": "issue_0_title", "label": "제목", "value": "로그인"},
            {"type": "text", "id": "issue_0_body", "label": "본문", "value": "설명", "multiline": True},
            {"type": "choice", "id": "issue_0_assignee", "label": "담당자", "value": "dev",
             "choices": [{"title": "개발자", "value": "dev"}]},
        ]}}
        serialized = json.loads(module._build_buttons_card(event).model_dump_json(by_alias=True, exclude_none=True))
        input_ids = [element.get("id") for element in serialized["body"] if element.get("id")]
        self.assertEqual(input_ids, ["issue_0_include", "issue_0_title", "issue_0_body", "issue_0_assignee"])
        self.assertEqual(len(serialized["actions"]), 2)

        result_event = {"title": "GitHub 이슈 등록 결과: 1건 생성, 1건 실패 - HTTP 403",
                        "buttons": [], "emphasis": True}
        result_card = json.loads(module._build_buttons_card(result_event).model_dump_json(
            by_alias=True, exclude_none=True))
        self.assertEqual(result_card.get("actions", []), [])
        self.assertTrue(any(element.get("text") == result_event["title"] for element in result_card["body"]))


if __name__ == "__main__":
    unittest.main()
