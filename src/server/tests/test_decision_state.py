"""브리핑·QA의 저장 성숙도 해석 동치. 업무 원문·실제 DB·실제 모델은 사용하지 않는다."""
from __future__ import annotations

import isolation  # noqa: F401,I001

import json
import unittest
from contextlib import closing

import prompts
from test_leading import Base, SCOPE
from worklog import db, decision_state, leading, qa


class DecisionStateTests(unittest.TestCase):
    def test_known_states_keep_their_meaning_and_unknown_is_not_disagreement(self):
        cases = [("draft", "draft", "가안"), ("recommended", "recommended", "조언"),
                 ("discussed", "discussed", "논의"), ("agreed", "agreed", "합의"),
                 (None, "unknown", "합의 여부 미확인"), ("", "unknown", "합의 여부 미확인"),
                 ("decided", "unknown", "합의 여부 미확인"), (True, "unknown", "합의 여부 미확인"),
                 (["agreed"], "unknown", "합의 여부 미확인"), ({"value": "agreed"}, "unknown", "합의 여부 미확인")]
        for value, expected, label in cases:
            with self.subTest(value=value):
                self.assertEqual(decision_state.resolve(value), {"value": expected, "label": label})

    def test_both_prompts_share_unknown_policy_without_changing_extraction_prompts(self):
        rule = prompts.load("common/decision-state").text
        for name in ("qa", "project-leading"):
            self.assertIn(rule, prompts.load(name).text)
        self.assertEqual(prompts.load("interpret").version, "part-extract-5")
        self.assertEqual(prompts.load("reconcile").version, "reconcile-2")


class DecisionStateIntegrationTests(Base):
    def seed(self, maturity=None, kind="decision", status=None):
        part_id = self.add_page("sample", "루트 > 기획 > 상태 기록", "document", "2026-10-02")
        item_id = self.make(kind, "알림 방식 검토", part_id, status=status)
        # 과거 값·미래 enum도 읽을 수 있어야 한다. 격리된 픽스처 DB에만 직접 넣는다.
        with db.write(self.store) as c:
            attrs = json.loads(c.execute("SELECT attributes_json FROM work_items WHERE id = ?", (item_id,)).fetchone()[0])
            attrs["maturity"] = maturity
            c.execute("UPDATE work_items SET attributes_json = ? WHERE id = ?", (json.dumps(attrs), item_id))
        return item_id

    def snapshot(self):
        with closing(db.connect(self.store)) as c:
            return ([tuple(r) for r in c.execute("SELECT id,status,version,attributes_json FROM work_items ORDER BY id")],
                    c.execute("SELECT COUNT(*) FROM revisions").fetchone()[0])

    def test_briefing_qa_payloads_and_rendering_agree_without_rewriting_legacy_data(self):
        cases = [(None, "unknown", "합의 여부 미확인"), ("draft", "draft", "가안"),
                 ("recommended", "recommended", "조언"), ("discussed", "discussed", "논의"),
                 ("agreed", "agreed", "합의"), (["agreed"], "unknown", "합의 여부 미확인")]
        for n, (stored, value, label) in enumerate(cases):
            for kind in ("decision", "requirement"):
                with self.subTest(stored=stored, kind=kind):
                    self.store = self.store.parent / f"state-{n}-{kind}.sqlite3"
                    db.initialize(self.store)
                    item_id = self.seed(stored, kind)
                    before = self.snapshot()
                    built = leading.briefing_items(self.store, SCOPE, {}, set())
                    item = next(i for i in built["aliased"].values() if i["id"] == item_id)
                    self.assertEqual(item["section"], "decided" if value == "agreed" else "unclear")
                    brief_payload = leading.briefing_payload(built, [])
                    ctx = qa.gather(self.store, SCOPE, "알림 방식")
                    payload = qa.build_payload(ctx, None)
                    expected = {"value": value, "label": label}
                    self.assertEqual(brief_payload["items"][0]["decision_state"], expected)
                    self.assertEqual(payload["items"][0]["decision_state"], expected)
                    self.assertEqual(payload["items"][0]["maturity"], None if value == "unknown" else value)
                    briefing = leading.render_briefing({"lines": [], "overview": None}, built)

                    calls = []
                    alias = payload["items"][0]["id"]
                    def fake_model(name, data):
                        calls.append(data)
                        return {"intent": "answer", "answer": "저장 내용을 확인했어요.",
                                "findings": [{"claim": "해당 항목이 기록돼 있어요.", "evidence_ids": [alias], "link_basis": "explicit"}],
                                "work_status": [], "searched_scope": [], "unknowns": [], "need": [], "next": []}, {}, "fake"
                    result = qa.ask(ctx, {"status": "skipped"}, fake_model)
                    self.assertEqual(result["status"], "ok")
                    self.assertEqual(result["calls"], 1)
                    self.assertEqual(len(calls), 1)
                    for text in (briefing, result["text"]):
                        self.assertIn(f"[{label}] 알림 방식 검토", text)
                        self.assertNotIn("확정, 성숙도 미기록", text)
                    self.assertEqual(self.snapshot(), before)

    def test_work_and_conflict_status_do_not_become_agreement(self):
        for n, (maturity, kind, status, assignee, github, section) in enumerate([
            (None, "decision", "active", None, {"key": "done"}, "unclear"),
            ("draft", "requirement", "active", None, {"key": "done"}, "unclear"),
            ("agreed", "decision", "needs_discussion", None, None, "unclear"),
            ("agreed", "open", "active", None, None, "unclear"),
            (None, "task", "open", "담당자", {"key": "done"}, "in_progress"),
        ]):
            with self.subTest(case=n):
                self.assertEqual(leading._section_of({"kind": kind, "status": status, "maturity": maturity,
                                                     "assignee": assignee, "github": github}), section)

    def test_qa_does_not_add_state_without_a_citable_finding(self):
        self.seed(None)
        ctx = qa.gather(self.store, SCOPE, "알림 방식")
        # 표시할 수 있는 finding이 없는 경우 입력 항목명이나 상태를 임의로 덧붙이지 않는다.
        def fake_model(name, payload):
            return {"intent": "answer", "answer": "모름", "findings": [], "work_status": [],
                    "searched_scope": [], "unknowns": [], "need": [], "next": []}, {}, "fake"
        result = qa.ask(ctx, {"status": "skipped"}, fake_model)
        self.assertNotIn("📋 의사결정 상태", result["text"])


if __name__ == "__main__":
    unittest.main()
