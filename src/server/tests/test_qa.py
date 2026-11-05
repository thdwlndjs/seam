"""Phase 5: 격리된 저장소와 가짜 모델·GitHub로 질문 흐름을 검증한다."""
from __future__ import annotations

import isolation  # noqa: F401,I001

import asyncio
import json
import unittest
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import main
import prompts
import worklog_bridge
from test_leading import Base, SCOPE
from worklog import board, core, db, explore, qa
from worklog.db import ContractError


def answer(**changes):
    result = {"intent": "answer", "answer": "확인한 내용을 정리했어요.", "findings": [], "work_status": [],
              "searched_scope": [], "unknowns": [], "need": [], "next": []}
    result.update(changes)
    return result


class QuestionTests(Base):
    def setUp(self):
        super().setUp()
        page = self.add_page("doc-1", "루트 > 기획 > 로그인", "document", "2026-10-02")
        explore.select_all(self.store, SCOPE, "doc-1", explore.HUMAN)
        old = self.make("decision", "로그인 방식 이전 결정", page, maturity="agreed", status="superseded")
        self.item = self.make("requirement", "로그인 방식 이메일", page, maturity="agreed",
                              refs=[{"type": "issue", "number": 7, "basis": "inferred"}])
        with db.write(self.store) as c:
            core._link(c, self.item, old, core.LINK_SUPERSEDES, None, db.now())
        self.ctx = qa.gather(self.store, SCOPE, "로그인 방식")
        self.alias = next(k for k, v in self.ctx["items"].items() if v["item_id"] == self.item)
        self.ctx["items"][self.alias]["quotes"] = ["이메일 방식으로 합의"]
        self.github = {"status": "skipped"}

    def run_answer(self, *responses, github=None):
        calls = []
        def call(name, payload):
            calls.append((name, payload))
            return responses[len(calls) - 1], {"prompt_tokens": 12, "completion_tokens": 4}, "fake"
        result = qa.ask(self.ctx, github or self.github, call)
        return result, calls

    def test_search_returns_related_decision_and_real_saved_source(self):
        item = self.ctx["items"][self.alias]
        self.assertTrue(any("대체" in r["rel"] for r in item["relations"]))
        self.assertEqual(item["sources"][0]["page_id"], "doc-1")
        self.assertEqual(item["refs"][0]["url"], "https://www.notion.so/doc1")
        self.assertEqual(self.ctx["searched"]["scope"], SCOPE)
        self.assertIn("searched_scope", prompts.load("qa").text)

    def test_resolution_relation_is_available_from_both_sides(self):
        pending = self.make("open", "로그인 수단 미정", self.ctx["items"][self.alias]["sources"][0]["part_id"])
        with db.write(self.store) as c:
            core._link(c, self.item, pending, core.LINK_RESOLVES, None, db.now())
        ctx = qa.gather(self.store, SCOPE, "로그인")
        decision = next(item for item in ctx["items"].values() if item["item_id"] == self.item)
        question = next(item for item in ctx["items"].values() if item["item_id"] == pending)
        self.assertTrue(any("해결함" in rel["rel"] for rel in decision["relations"]))
        self.assertTrue(any("해결한 항목" in rel["rel"] for rel in question["relations"]))

    def test_unselected_document_is_reported_as_unread(self):
        self.add_page("doc-2", "루트 > 비공개 영역", "document", "2026-10-02")
        ctx = qa.gather(self.store, SCOPE, "로그인 방식")
        self.assertEqual(ctx["not_interpreted"]["pages"], 1)
        self.assertIn("확인하지 않았어요", " ".join(qa.scope_lines(ctx, self.github)))

    def test_section_links_point_to_recorded_block_and_fallback_to_page(self):
        self.assertEqual(board._section_url("ab-cd", "notion", '["12-34"]'), "https://www.notion.so/abcd#1234")
        self.assertEqual(board._section_url("ab-cd", "notion", "[]"), "https://www.notion.so/abcd")
        self.assertEqual(board._section_url("ab-cd", "notion", '{"invalid": 1}'), "https://www.notion.so/abcd")
        self.assertIsNone(board._section_url("ab-cd", "upload", '["12-34"]'))

    def test_answer_cites_only_known_evidence_and_scope_comes_from_code(self):
        result, calls = self.run_answer(answer(searched_scope=["확인하지 않은 자료"], findings=[
            {"claim": "이메일 방식을 합의했어요", "evidence_ids": [self.alias], "location": "문서", "link_basis": "explicit"},
            {"claim": "근거 없는 주장", "evidence_ids": ["k999"], "location": "", "link_basis": "explicit"}]))
        self.assertEqual(len(calls), 1)
        self.assertIn("이메일 방식을 합의했어요", result["text"])
        self.assertNotIn("근거 없는 주장", result["text"])
        self.assertNotIn("확인하지 않은 자료", result["text"])
        self.assertEqual(result["scope"], qa.scope_lines(self.ctx, self.github))
        self.assertTrue(result["links"][0]["url"].startswith("https://www.notion.so/"))

    def test_need_allows_known_quote_only_and_stops_after_second_call(self):
        first = answer(need=[{"type": "item_source", "id": self.alias}, {"type": "item_source", "id": "k999"}])
        second = answer(intent="coordination", need=[{"type": "unknown", "id": "ignored"}] * 6)
        result, calls = self.run_answer(first, second)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1][1]["extra"][0]["quotes"], ["이메일 방식으로 합의"])
        self.assertIn("k999", result["text"])
        self.assertNotIn("조율은 아직 준비 중", result["text"])

    def test_coordination_compares_alternative_within_two_calls(self):
        self.assertIn("만들면서 QA", prompts.load("qa").text)
        self.ctx["question"] = "만들면서 QA를 하는 대안은 어때요?"
        second = {"now": [], "later": [], "cross_role": [], "parallel": [],
                  "compared_alternatives": [
                      {"option": "현재 안", "fits_when": "먼저 범위를 정할 때", "tradeoffs": "결정을 기다려야 해요"},
                      {"option": "만들면서 QA", "fits_when": "짧은 피드백을 병행할 때", "tradeoffs": "변경 가능성을 살펴야 해요"}],
                  "methods_used": ["product-discovery"], "need_method": "unknown-and-ignored"}
        result, calls = self.run_answer(answer(intent="coordination", need=[{"type": "item_source", "id": self.alias}]), second)
        self.assertEqual([name for name, _ in calls], ["qa", "coordination"])
        self.assertEqual(result["calls"], 2)
        self.assertIn("만들면서 QA", result["text"])
        self.assertIn("현재 안", result["text"])
        self.assertNotIn("조율은 아직 준비 중", result["text"])
        self.assertNotIn("저장된 근거로 질문에 대한 답을 확인하지 못했어요.", result["text"])

    def test_question_coordination_turn_uses_real_saved_context_and_buttons(self):
        turn = SessionTests().turn(text="로그인 방식은 만들면서 QA를 하면 어때요?")
        config = {"notion_page_id": "root", "notion_token": "fake"}
        calls = []
        def fake_model(name, payload):
            calls.append(name)
            if name == "qa":
                alias = next(i["id"] for i in payload["items"] if i["title"] == "로그인 방식 이메일")
                return answer(intent="coordination", findings=[{"claim": "기록된 로그인 방식이 있어요",
                              "evidence_ids": [alias], "link_basis": "explicit"}]), {}, "fake"
            item = next(i for i in payload["context"]["items"] if i["id"] == self.item)
            self.assertEqual(payload["first"]["findings"][0]["evidence_ids"], [self.item])
            result = {"now": [{"item_id": item["id"], "suggestion": "방식을 같이 검토해요",
                               "reason": "로그인 방식이 기록돼 있어요", "roles": [], "evidence_ids": [item["id"]]}],
                      "later": [], "cross_role": [], "parallel": [],
                      "compared_alternatives": [{"option": "현재 안", "fits_when": "범위를 먼저 정할 때", "tradeoffs": "확인이 필요해요"},
                                                {"option": "만들면서 QA", "fits_when": "피드백을 병행할 때", "tradeoffs": "변경을 살펴야 해요"}],
                      "methods_used": [payload["methods"][0]["id"]], "need_method": "spike"}
            return result, {}, "fake"
        with patch.object(worklog_bridge, "WORKLOG_STORE", self.store), patch.object(worklog_bridge, "lead_call", fake_model), \
             patch.object(main, "_qa_github", new=AsyncMock(return_value={"status": "skipped"})):
            asyncio.run(main._handle_qa(turn, config, turn.req.text))
        events = []
        while not turn.queue.empty():
            events.append(turn.queue.get_nowait())
        self.assertEqual(calls, ["qa", "coordination"])
        self.assertTrue(any("만들면서 QA" in e.get("text", "") for e in events))
        actions = [b.get("data", {}).get("action") for e in events if e["type"] == "buttons" for b in e["buttons"]]
        self.assertIn("coord_decision", actions)
        self.assertFalse(any(i["kind"] == "decision" and i["title"] == "방식을 같이 검토해요"
                             for i in board.snapshot(self.store, SCOPE)["items"]))

    def test_invalid_contract_fails_without_retry_and_reports_scope(self):
        for bad in (answer(intent="other"), answer(need=[{"type": "github", "id": "x"}] * 6),
                    {k: v for k, v in answer().items() if k != "searched_scope"}):
            with self.subTest(bad=bad):
                result, calls = self.run_answer(bad)
                self.assertEqual(result["status"], "failed")
                self.assertEqual(len(calls), 1)
                self.assertIn("조회 범위", result["text"])

    def test_unknown_need_does_not_trigger_another_call(self):
        result, calls = self.run_answer(answer(need=[{"type": "github", "id": "issue#999"}]))
        self.assertEqual(len(calls), 1)
        self.assertIn("확인하지 않았어요", result["text"])

    def test_failed_provider_attempt_counts_as_a_call(self):
        def broken(name, payload):
            raise RuntimeError("provider unavailable")
        result = qa.ask(self.ctx, self.github, broken)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["calls"], 1)

    def test_unavailable_sources_cannot_be_presented_as_links_or_evidence(self):
        self.ctx["items"][self.alias]["refs"][0]["url"] = None
        result, _ = self.run_answer(answer(answer="근거 없는 확정 답", findings=[
            {"claim": "확정", "evidence_ids": [self.alias], "location": "", "link_basis": "explicit"}]))
        self.assertNotIn("근거 없는 확정 답", result["text"])
        self.assertEqual(result["links"], [])
        self.assertIn("확인하지 못했어요", result["text"])

    def test_superseded_item_does_not_create_a_broken_board_deep_link(self):
        self.ctx["items"][self.alias]["status"] = "superseded"
        result, _ = self.run_answer(answer(findings=[{"claim": "이전 결정", "evidence_ids": [self.alias],
                                                       "location": "", "link_basis": "explicit"}]))
        self.assertIsNone(result["view_item"])

    def test_github_item_outside_lookup_is_not_a_known_fact(self):
        pack = qa.github_pack(self.ctx, "sample/repo", [], [], set(), {})
        github = {"status": "ok", "pack": pack}
        result, calls = self.run_answer(answer(need=[{"type": "github", "id": "issue#7"}],
                                               work_status=[{"ref": "issue#7"}]), github=github)
        self.assertEqual(len(calls), 1)
        self.assertIn("조회 범위 밖", result["text"])
        self.assertEqual(result["links"], [])

    def test_github_states_files_and_inference_propagation(self):
        pack = qa.github_pack(self.ctx, "sample/repo", [{"number": 7, "state": "closed"}],
                              [{"number": 9, "title": "Fix #7", "body": "", "state": "closed", "merged_at": "2026-01-01"}],
                              set(), {9: ["src/login.py"]}, 2)
        facts = {f["ref"]: f for f in pack["facts"]}
        self.assertEqual(facts["issue#7"]["basis"], "inferred")
        self.assertEqual(facts["pr#9"]["basis"], "inferred")
        result, _ = self.run_answer(answer(work_status=[{"ref": "pr#9"}]), github={"status": "ok", "pack": pack})
        self.assertIn("병합됨", result["text"])
        self.assertIn("src/login.py", result["text"])
        self.assertIn("추정 연결", result["text"])
        self.assertIn("동작 검증은 확인하지 않았어요", result["text"])

    def test_changed_file_does_not_prove_implementation_or_code_location(self):
        pack = qa.github_pack(self.ctx, "sample/repo", [{"number": 7, "state": "closed"}],
                              [{"number": 9, "title": "Fix #7", "state": "closed", "merged_at": "2026-01-01"}],
                              set(), {9: ["src/login.py"]})
        pr = next(f for f in pack["facts"] if f["ref"] == "pr#9")
        for claim in ("기능이 구현된 것으로 보입니다.", "코드는 src/login.py 파일에 있습니다."):
            result, _ = self.run_answer(answer(answer=claim, findings=[{"claim": claim, "evidence_ids": [pr["id"]],
                                                                         "location": "", "link_basis": "explicit"}]),
                                        github={"status": "ok", "pack": pack})
            self.assertNotIn(claim, result["text"])
            self.assertIn("변경 파일: src/login.py", result["text"])
            self.assertIn("동작 검증은 확인하지 않았어요", result["text"])

    def test_payload_never_silently_exceeds_budget(self):
        with self.assertRaises(ContractError):
            qa.build_payload(self.ctx, None, extra=[{"quotes": ["큰 원문" * 20000]}], budget_tokens=100)

    def test_input_budget_includes_system_prompt(self):
        payload = qa.build_payload(self.ctx, None)
        cost = core.estimate_tokens(prompts.load("qa").text) + core.estimate_tokens(json.dumps(payload, ensure_ascii=False))
        self.assertLessEqual(cost, qa.QA_BUDGET_TOKENS)
        with patch.object(qa.prompts, "load", return_value=SimpleNamespace(text="가" * 10000)):
            with self.assertRaises(ContractError):
                qa.build_payload(self.ctx, None)

    def test_model_cannot_cite_an_item_trimmed_from_its_input(self):
        raw = answer(answer="빠진 항목의 주장", findings=[{"claim": "숨긴 항목", "evidence_ids": [self.alias],
                                                           "location": "", "link_basis": "explicit"}])
        with patch.object(qa, "build_payload", return_value={"question": "질문", "items": [], "github": None}):
            result, _ = self.run_answer(raw)
        self.assertNotIn("숨긴 항목", result["text"])
        self.assertNotIn("빠진 항목의 주장", result["text"])

    def test_agenda_is_only_created_on_action(self):
        result, _ = self.run_answer(answer())
        with closing(db.connect(self.store)) as c:
            before = c.execute("SELECT COUNT(*) FROM work_items").fetchone()[0]
        button = result["buttons"][0]["data"]
        created = qa.create_agenda(self.store, SCOPE, button["text"], button["mode"], "사람")
        with closing(db.connect(self.store)) as c:
            row = c.execute("SELECT attributes_json FROM work_items WHERE id = ?", (created["id"],)).fetchone()
            self.assertEqual(c.execute("SELECT COUNT(*) FROM work_items").fetchone()[0], before + 1)
        self.assertTrue(json.loads(row[0])["agenda"])

    def test_end_to_end_question_uses_saved_knowledge_without_notion(self):
        config = {"notion_page_id": "root", "notion_token": "fake"}
        turn = SessionTests().turn(text="로그인 방식 결정됐어?")
        async def no_github(*args):
            return {"status": "skipped"}
        def fake_model(name, payload):
            alias = next(i["id"] for i in payload["items"] if i["title"] == "로그인 방식 이메일")
            return answer(findings=[{"claim": "이메일 방식으로 합의", "evidence_ids": [alias], "location": "문서", "link_basis": "explicit"}]), {}, "fake"
        with patch.object(worklog_bridge, "WORKLOG_STORE", self.store), patch.object(worklog_bridge, "lead_call", fake_model), \
             patch.object(main, "_qa_github", no_github), patch.object(main, "_sync_team_map") as notion:
            asyncio.run(main._handle_qa(turn, config, turn.req.text))
        events = []
        while not turn.queue.empty():
            events.append(turn.queue.get_nowait())
        self.assertTrue(any("이메일 방식으로 합의" in e.get("text", "") for e in events))
        self.assertTrue(any(e["type"] == "buttons" for e in events))
        notion.assert_not_called()


class SessionTests(unittest.TestCase):
    def setUp(self):
        main.qa_sessions.clear()

    def tearDown(self):
        main.qa_sessions.clear()

    def turn(self, team="T1", text="", value=None):
        return main.Turn(main.TurnRequest(user={"aad_id": "u", "name": "사용자"}, team={"id": team, "name": "팀"},
                                          text=text, value=value, conversation={"id": "C1", "type": "channel"},
                                          message_id="m", sent_at="2026-11-05T00:00:00Z"))

    def test_session_expires(self):
        with patch.object(main.time, "monotonic", return_value=1000):
            main.qa_sessions["u"] = {"team_key": SCOPE, "at": 399}
            self.assertFalse(main.has_qa_session("u"))

    def test_question_turn_does_not_schedule_notion_sync(self):
        config = {"notion_page_id": "root", "notion_token": "fake"}
        with patch.object(main, "_get_config", return_value=config), patch.object(main, "_sync_member"), \
             patch.object(main, "_check_on_open") as sync, patch.object(main, "_handle_qa", new=AsyncMock()) as handle:
            asyncio.run(main.handle_turn(self.turn(value={"action": "qa_start"})))
            asyncio.run(main.handle_turn(self.turn(text="결정됐어?")))
            self.assertEqual(handle.await_count, 1)
            sync.assert_not_called()

    def test_question_session_does_not_follow_user_into_another_team(self):
        main.qa_sessions["u"] = {"team_key": SCOPE, "at": main.time.monotonic()}
        with patch.object(main, "_handle_qa", new=AsyncMock()) as handle:
            asyncio.run(main._handle_text(self.turn(team="T2", text="질문"), {}, "질문", "u"))
            handle.assert_not_awaited()

    def test_selecting_menu_button_ends_question_session(self):
        main.qa_sessions["u"] = {"team_key": SCOPE, "at": main.time.monotonic()}
        with patch.object(main, "_run_action", new=AsyncMock()) as run_menu:
            asyncio.run(main._dispatch_value(self.turn(), {"action": "menu", "choice": "2"}))
        run_menu.assert_awaited_once()
        self.assertFalse(main.has_qa_session("u"))

    def test_github_requests_are_limited_to_relevant_pr_paths(self):
        ctx = {"items": {"k1": {"github": [{"type": "issue", "number": 7, "basis": "explicit"}]}}}
        async def get(repo, path, token, params):
            if path == "/issues":
                return [{"number": 7, "state": "closed"}]
            if path == "/pulls":
                return [{"number": 9, "title": "Fix #7", "state": "closed", "merged_at": "2026-01-01"}]
            if path == "/commits":
                return []
            if path == "/pulls/9/files":
                return [{"filename": "src/login.py"}]
            raise AssertionError(path)
        with patch.object(main, "_github_get_cached", new=AsyncMock(side_effect=get)) as get_mock:
            data = asyncio.run(main._qa_github({"github_token": "fake", "github_repo": "owner/repo"}, ctx, "구현됐어?"))
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["pack"]["files_prs"], [9])
        self.assertEqual(get_mock.await_count, 4)

    def test_github_is_queried_for_work_not_for_decision_wording_alone(self):
        self.assertFalse(qa.wants_github("요구사항이 결정됐나요?"))
        self.assertFalse(qa.wants_github("결정 근거가 어디에 있어요?"))
        self.assertTrue(qa.wants_github("이 기능은 어디에 있어요?"))
        self.assertTrue(qa.wants_github("구현이 완료됐나요?"))
        self.assertTrue(qa.wants_github("PR은 어떻게 됐나요?"))
        self.assertFalse(qa.wants_github("approval에 관한 결정은?"))


class ProviderTests(unittest.TestCase):
    def test_waiting_user_invalid_json_does_not_send_a_second_request(self):
        for prompt_name in ("qa", "coordination"):
            with self.subTest(prompt=prompt_name):
                provider = SimpleNamespace(name="fake", url="https://example.invalid", key="fake", model="fake")
                response = MagicMock(status_code=200, headers={})
                response.json.return_value = {"choices": [{"message": {"content": "not json"}}]}
                client = MagicMock()
                client.__enter__.return_value.post.return_value = response
                with patch.object(worklog_bridge, "GROQ_API_KEY", "fake"), \
                     patch.object(worklog_bridge.llm_queue, "available", return_value=[provider]), \
                     patch.object(worklog_bridge.llm_queue, "record"), patch.object(worklog_bridge.httpx, "Client", return_value=client):
                    with self.assertRaises(ContractError):
                        worklog_bridge.lead_call(prompt_name, {"question": "가상 질문"})
                self.assertEqual(client.__enter__.return_value.post.call_count, 1)

    def test_rate_limit_uses_another_provider_within_one_call(self):
        providers = [SimpleNamespace(name=n, url="https://example.invalid/" + n, key="fake", model="fake") for n in ("a", "b")]
        limited = MagicMock(status_code=429, headers={})
        limited.json.return_value = {}
        good = MagicMock(status_code=200, headers={})
        good.json.return_value = {"choices": [{"message": {"content": '{"ok": true}'}}]}
        client = MagicMock()
        client.__enter__.return_value.post.side_effect = [limited, good]
        with patch.object(worklog_bridge.llm_queue, "available", return_value=providers), \
             patch.object(worklog_bridge.llm_queue, "record"), patch.object(worklog_bridge.httpx, "Client", return_value=client):
            data, _, label = worklog_bridge._llm_call("prompt", {}, worklog_bridge.llm_queue.USER, retry_invalid_json=False)
        self.assertTrue(data["ok"])
        self.assertEqual(label, "b:fake")
        self.assertEqual(client.__enter__.return_value.post.call_count, 2)


if __name__ == "__main__":
    unittest.main()
