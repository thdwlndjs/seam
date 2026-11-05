"""Phase 6: 격리 저장소와 가짜 모델로 조율 동작을 검증한다."""
from __future__ import annotations

import isolation  # noqa: F401
import asyncio
import json
import unittest
from contextlib import closing
from unittest.mock import AsyncMock, patch

import main
import prompts
import worklog_bridge
from test_leading import Base, SCOPE
from worklog import board, coordination, core, db, leading, people, qa


class MethodSelectionTests(unittest.TestCase):
    def test_unresolved_hypothesis_selects_discovery_and_spike(self):
        context = {"items": [{"kind": "hypothesis", "title": "결제 가설", "status": "active", "relations": []}],
                   "question": ""}
        self.assertEqual(coordination.select_methods(context), ["product-discovery", "spike"])

    def test_signal_rules_and_method_files_are_bounded(self):
        for title, expected in [("의존 리스크", "raid"), ("화면 프로토타입", "design-sprint"), ("팀 회고", "scrum-inspect-adapt")]:
            with self.subTest(title=title):
                chosen = coordination.select_methods({"items": [{"title": title, "relations": []}], "question": ""})
                self.assertEqual(chosen[0], expected)
                self.assertLessEqual(len(chosen), 2)
        for method in coordination.METHODS:
            with self.subTest(method=method):
                text = prompts.load(f"methods/{method}").text
                for heading in ("적용 조건", "핵심 판단 질문", "예상 산출물", "비적용 조건", "원문 출처"):
                    self.assertIn(heading, text)
                self.assertIn("https://", text)
                self.assertLessEqual(core.estimate_tokens(text), 400)

    def test_invalid_evidence_or_forced_process_is_rejected(self):
        context = {"items": [{"id": "i1"}], "people": []}
        proposal = {"item_id": "i1", "suggestion": "확인해요", "reason": "기록", "roles": [], "evidence_ids": ["i1"]}
        raw = {"now": [proposal], "later": [], "cross_role": [], "parallel": [], "compared_alternatives": [],
               "methods_used": ["raid"], "need_method": None}
        for bad in (dict(proposal, evidence_ids=["unknown"]), dict(proposal, roles=["없는 역할"]),
                    dict(proposal, suggestion="반드시 이 과정을 따라야 해요")):
            with self.subTest(bad=bad), self.assertRaises(db.ContractError):
                coordination.validate(dict(raw, now=[bad]), context, ["raid"])
        with self.assertRaises(db.ContractError):
            coordination.validate(dict(raw, methods_used=[]), context, ["raid"])
        context["items"][0]["assignee"] = "기록된 담당자"
        allowed = dict(proposal, roles=["기록된 담당자"])
        self.assertEqual(coordination.validate(dict(raw, now=[allowed]), context, ["raid"])["now"][0]["roles"],
                         ["기록된 담당자"])

    def test_four_coordination_sections_show_roles_sources_and_methods(self):
        items = [{"id": f"i{n}", "kind": "open", "title": f"항목 {n}", "statement": "저장 기록",
                  "status": "active", "relations": [], "refs": [{"label": f"회의 {n}", "url": f"https://example.test/{n}"}]}
                 for n in range(1, 5)]
        context = {"items": items, "question": "", "people": [{"name": "팀원", "role": "dev"}], "stage": None}
        def model(name, payload):
            section = lambda n: [{"item_id": f"i{n}", "suggestion": f"제안 {n}", "reason": "기록된 내용을 확인",
                                  "roles": ["개발"], "evidence_ids": [f"i{n}"]}]
            return {"now": section(1), "later": section(2), "cross_role": section(3), "parallel": section(4),
                    "compared_alternatives": [], "methods_used": [payload["methods"][0]["id"]], "need_method": None}, {}, "fake"
        outcome = coordination.propose(context, model)
        for expected in ("지금 함께 정할 것", "나중에 정할 것", "역할 간 조율", "병렬 진행", "개발", "회의 1",
                         "https://example.test/1", "Product Discovery"):
            self.assertIn(expected, outcome["text"])
        self.assertEqual(outcome["calls"], 1)

    def test_one_item_can_need_action_now_and_cross_role_discussion(self):
        context = {"items": [{"id": "i1"}], "people": [{"name": "팀원", "role": "dev"}]}
        entry = {"item_id": "i1", "suggestion": "함께 확인", "reason": "연결된 기록",
                 "roles": ["개발"], "evidence_ids": ["i1"]}
        raw = {"now": [entry], "later": [], "cross_role": [entry], "parallel": [], "compared_alternatives": [],
               "methods_used": ["raid"], "need_method": None}
        result = coordination.validate(raw, context, ["raid"])
        self.assertEqual(len(result["now"]), 1)
        self.assertEqual(len(result["cross_role"]), 1)
        with self.assertRaises(db.ContractError):
            coordination.validate(dict(raw, later=[entry]), context, ["raid"])

    def test_menu_uses_at_most_two_sources_and_follows_one_method_request(self):
        context = {"items": [{"id": "i1", "kind": "hypothesis", "title": "가설", "status": "active",
                              "statement": "사용자 가설", "relations": [], "refs": [{"label": "회의", "url": "https://example.test"}]}],
                   "question": "", "people": [], "stage": None}
        calls = []
        def model(name, payload):
            calls.append((name, payload))
            result = {"now": [{"item_id": "i1", "suggestion": "가설을 함께 확인", "reason": "가설이 남아 있음",
                               "roles": [], "evidence_ids": ["i1"]}],
                      "later": [], "cross_role": [], "parallel": [], "compared_alternatives": [],
                      "methods_used": [payload["methods"][0]["id"]], "need_method": "raid" if len(calls) == 1 else "spike"}
            return result, {"prompt_tokens": 12, "completion_tokens": 3}, "fake"
        outcome = coordination.propose(context, model)
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(len(payload["methods"]) <= 2 for _, payload in calls))
        self.assertEqual(calls[1][1]["methods"][-1]["id"], "raid")
        self.assertEqual(outcome["calls"], 2)
        self.assertIn("가설을 함께 확인", outcome["text"])


class CoordinationActionTests(Base):
    def setUp(self):
        super().setUp()
        part = self.add_page("doc-1", "루트 > 기획 > 결제", "document", "2026-10-02")
        self.item = self.make("open", "결제 방식 선택", part, maturity="discussed")

    def tearDown(self):
        main.coord_sessions.clear()
        main.qa_sessions.clear()
        super().tearDown()

    def turn(self):
        return main.Turn(main.TurnRequest(user={"aad_id": "u", "name": "사용자"},
                                          team={"id": "T1", "name": "팀"}, text="",
                                          conversation={"id": "C1", "type": "channel"}, message_id="m",
                                          sent_at="2026-11-05T00:00:00Z"))

    def test_agenda_and_defer_require_explicit_action_and_are_visible(self):
        context = coordination.gather(self.store, SCOPE)
        self.assertTrue(any(i["id"] == self.item for i in context["items"]))
        before = board.snapshot(self.store, SCOPE)
        self.assertFalse(next(i for i in before["items"] if i["id"] == self.item)["agenda"])
        coordination.act(self.store, SCOPE, self.item, "agenda", "사용자")
        item = next(i for i in board.snapshot(self.store, SCOPE)["items"] if i["id"] == self.item)
        self.assertTrue(item["agenda"])
        self.assertFalse(item["deferred"])
        coordination.act(self.store, SCOPE, self.item, "defer", "사용자")
        item = next(i for i in board.snapshot(self.store, SCOPE)["items"] if i["id"] == self.item)
        self.assertTrue(item["deferred"])
        self.assertFalse(any(i["id"] == self.item for i in coordination.agendas(self.store, SCOPE)))
        coordination.act(self.store, SCOPE, self.item, "resume", "사용자")
        self.assertTrue(any(i["id"] == self.item for i in coordination.agendas(self.store, SCOPE)))

    def test_recorded_decision_is_only_human_input_and_has_human_evidence(self):
        typed = "결제 방식은 계좌이체로 정합니다."
        made = coordination.record_decision(self.store, SCOPE, self.item, typed, "사용자")
        item = next(i for i in board.snapshot(self.store, SCOPE)["items"] if i["id"] == made["id"])
        self.assertEqual(item["statement"], typed)
        self.assertEqual(item["maturity"], "agreed")
        self.assertIn(typed, item["quotes"])
        with closing(db.connect(self.store)) as c:
            source = c.execute("""SELECT s.kind, s.content FROM sources s JOIN evidence e ON e.source_id=s.id
                                  JOIN object_evidence oe ON oe.evidence_id=e.id
                                  WHERE oe.object_type='work_item' AND oe.object_id=?""", (made["id"],)).fetchone()
        self.assertEqual((source["kind"], source["content"]), ("teams_message", typed))

    def test_cross_team_and_blank_decision_cannot_modify_items(self):
        with self.assertRaises(db.NotFoundError):
            coordination.act(self.store, "team:other", self.item, "agenda", "사용자")
        with self.assertRaises(db.ValidationError):
            coordination.record_decision(self.store, SCOPE, self.item, "  ", "사용자")
        self.assertEqual(len(board.snapshot(self.store, SCOPE)["items"]), 1)

    def test_known_github_work_state_keeps_its_issue_link(self):
        part_id = board.snapshot(self.store, SCOPE)["items"][0]["sources"][0]["part_id"]
        item_id = self.make("task", "결제 구현", part_id, assignee="개발자",
                            refs=[{"type": "issue", "number": 7, "basis": "explicit"}])
        ctx = coordination.gather(self.store, SCOPE, {7: "closed"}, set(), repo="example/project")
        item = next(i for i in ctx["items"] if i["id"] == item_id)
        self.assertEqual(item["github"]["key"], "done")
        self.assertTrue(any(ref["url"] == "https://github.com/example/project/issues/7" for ref in item["refs"]))

    def test_related_task_and_team_role_survive_a_large_input(self):
        part_id = board.snapshot(self.store, SCOPE)["items"][0]["sources"][0]["part_id"]
        task = self.make("task", "연결된 개발 작업", part_id, assignee="개발자")
        with db.write(self.store) as c:
            core._link(c, self.item, task, core.LINK_AFFECTS, None, db.now())
        for n in range(25):
            self.make("open", f"추가 미정 {n:02d}", part_id, statement="긴 설명 " * 150)
        people.touch(self.store, SCOPE, "dev-user", "개발자")
        people.set_role(self.store, "dev-user", "dev")
        ctx = coordination.gather(self.store, SCOPE)
        self.assertLessEqual(len(ctx["items"]), 24)
        self.assertTrue(any(i["id"] == task for i in ctx["items"]))
        self.assertTrue(any(p["name"] == "개발자" and p["role"] == "dev" for p in ctx["people"]))
        shown = []
        def model(name, payload):
            shown.extend(i["id"] for i in payload["context"]["items"])
            return {"now": [], "later": [], "cross_role": [], "parallel": [], "compared_alternatives": [],
                    "methods_used": [], "need_method": None}, {}, "fake"
        outcome = coordination.propose(ctx, model)
        self.assertIn(task, shown)
        self.assertGreater(outcome["context"]["omitted_items"], 0)
        self.assertIn("입력 한도로", outcome["text"])

    def test_decision_button_waits_for_user_text_before_creating_decision(self):
        turn = self.turn()
        config = {"notion_page_id": "root", "notion_token": "fake"}
        with patch.object(main, "_get_config", return_value=config), patch.object(worklog_bridge, "WORKLOG_STORE", self.store):
            self.assertTrue(asyncio.run(main._dispatch_value(turn, {"action": "coord_decision", "item_id": self.item})))
            self.assertFalse(any(i["kind"] == "decision" for i in board.snapshot(self.store, SCOPE)["items"]))
            asyncio.run(main._handle_text(turn, config, "결제 방식은 계좌이체로 정합니다.", "u"))
        decisions = [i for i in board.snapshot(self.store, SCOPE)["items"] if i["kind"] == "decision"]
        self.assertEqual([d["statement"] for d in decisions], ["결제 방식은 계좌이체로 정합니다."])

    def test_menu_proposal_buttons_do_not_create_items_until_chosen(self):
        turn = self.turn()
        config = {"notion_page_id": "root", "notion_token": "fake"}
        calls = []
        def model(name, payload):
            calls.append(name)
            return {"now": [{"item_id": self.item, "suggestion": "결제 방식을 논의해요",
                             "reason": "결제 방식이 미정이에요", "roles": [], "evidence_ids": [self.item]}],
                    "later": [], "cross_role": [], "parallel": [], "compared_alternatives": [],
                    "methods_used": [payload["methods"][0]["id"]], "need_method": None}, {}, "fake"
        with patch.object(main, "_get_config", return_value=config), patch.object(worklog_bridge, "WORKLOG_STORE", self.store), \
             patch.object(worklog_bridge, "lead_call", model):
            self.assertTrue(asyncio.run(main._dispatch_value(turn, {"action": "coord_start"})))
            items = board.snapshot(self.store, SCOPE)["items"]
            self.assertEqual(len(items), 1)
            self.assertFalse(items[0]["agenda"])
            events = []
            while not turn.queue.empty():
                events.append(turn.queue.get_nowait())
            actions = [b["data"]["action"] for e in events if e["type"] == "buttons" for b in e["buttons"]]
            self.assertEqual(actions, ["coord_agenda", "coord_defer", "coord_decision"])
            self.assertTrue(any("Product Discovery" in e.get("text", "") for e in events))
            asyncio.run(main._dispatch_value(turn, {"action": "coord_agenda", "item_id": self.item}))
            with patch.object(main, "fetch_team_status", new=AsyncMock(return_value=("📎 근거", []))), \
                 patch.object(main, "_get_github_id", return_value=None):
                asyncio.run(main._run_menu(turn, "2"))
        events = []
        while not turn.queue.empty():
            events.append(turn.queue.get_nowait())
        self.assertTrue(any("다음 회의 안건" in e.get("text", "") and "결제 방식 선택" in e["text"] for e in events))
        self.assertTrue(next(i for i in board.snapshot(self.store, SCOPE)["items"] if i["id"] == self.item)["agenda"])
        self.assertEqual(calls, ["coordination"])

    def test_invalid_coordination_result_falls_back_to_briefing(self):
        turn = self.turn()
        config = {"notion_page_id": "root", "notion_token": "fake"}
        with patch.object(main, "_get_config", return_value=config), patch.object(worklog_bridge, "WORKLOG_STORE", self.store), \
             patch.object(worklog_bridge, "lead_call", return_value=({"now": []}, {}, "fake")) as call:
            asyncio.run(main._handle_coordination(turn))
        events = []
        while not turn.queue.empty():
            events.append(turn.queue.get_nowait())
        self.assertTrue(any("기존 브리핑" in e.get("text", "") and "결제 방식 선택" in e["text"] for e in events))
        self.assertEqual(call.call_count, 1)
        self.assertFalse(any(i["kind"] == "decision" for i in board.snapshot(self.store, SCOPE)["items"]))

    def test_switching_to_qa_cancels_pending_decision_input(self):
        turn = self.turn()
        config = {"notion_page_id": "root", "notion_token": "fake"}
        with patch.object(main, "_get_config", return_value=config), patch.object(worklog_bridge, "WORKLOG_STORE", self.store):
            asyncio.run(main._dispatch_value(turn, {"action": "coord_decision", "item_id": self.item}))
            asyncio.run(main._dispatch_value(turn, {"action": "qa_start"}))
        self.assertNotIn("u", main.coord_sessions)

    def test_recorded_decision_is_protected_from_automatic_overwrite(self):
        made = coordination.record_decision(self.store, SCOPE, self.item, "결제 방식을 직접 결정합니다.", "사용자")
        item = next(i for i in board.snapshot(self.store, SCOPE)["items"] if i["id"] == made["id"])
        self.assertTrue(item["human_edited"])

    def test_human_decision_appears_in_briefing_and_question_with_evidence(self):
        typed = "결제 방식은 계좌이체로 정합니다."
        made = coordination.record_decision(self.store, SCOPE, self.item, typed, "사용자")
        briefing = leading.briefing_items(self.store, SCOPE, {}, set())
        recorded = next(i for i in briefing["aliased"].values() if i["id"] == made["id"])
        self.assertEqual(recorded["section"], "decided")
        self.assertTrue(any(ref["label"] == "사람이 직접 기록" for ref in recorded["refs"]))
        ctx = qa.gather(self.store, SCOPE, "계좌이체")
        alias = next(a for a, i in ctx["items"].items() if i["item_id"] == made["id"])
        def model(name, payload):
            return {"intent": "answer", "answer": "기록을 확인했어요.",
                    "findings": [{"claim": typed, "evidence_ids": [alias], "link_basis": "explicit"}],
                    "work_status": [], "searched_scope": [], "unknowns": [], "need": [], "next": []}, {}, "fake"
        outcome = qa.ask(ctx, {"status": "skipped"}, model, tab_link=lambda item_id: f"https://example.test/board/{item_id}")
        self.assertIn(typed, outcome["text"])
        self.assertTrue(any(link["kind"] == "human" and made["id"] in link["url"] for link in outcome["links"]))
        ctx["items"][alias]["status"] = "superseded"
        hidden = qa.ask(ctx, {"status": "skipped"}, model, tab_link=lambda item_id: f"https://example.test/board/{item_id}")
        self.assertNotIn(typed, hidden["text"])
        self.assertEqual(hidden["links"], [])


if __name__ == "__main__":
    unittest.main()
