"""v2.1 Phase 2: 산출물 해석과 증분 축적. 문서: docs/implementation/v2.1/plan.md Phase 2.
모든 데이터는 지어낸 것이다(공개 저장소)."""

from __future__ import annotations

import isolation  # noqa: F401,I001  실제 저장소·키를 쓰지 않게(맨 먼저)

import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from unittest.mock import patch  # noqa: E402

import confirm_flow  # noqa: E402
import meeting_inputs  # noqa: E402
import worklog_bridge  # noqa: E402
from worklog import apply, core, db, explore, lineage, parts, runs, sections  # noqa: E402
from worklog.db import ContractError  # noqa: E402

SCOPE = "team:beta"
V = "p2-test"
EDITED = "2026-11-01T00:00:00Z"
ME = "테스터"


class Busy(Exception):
    pass


class Scripted:
    """원문에 구절이 있으면 정해 둔 항목을 돌려주는 가짜 해석기. artifact를 주면 문서 해석처럼 답한다."""

    def __init__(self, rules: dict[str, list[dict]], artifact: dict | None = None) -> None:
        self.rules, self.artifact, self.calls = rules, artifact, []

    def __call__(self, payload: dict) -> dict:
        self.calls.append(payload)
        items = [dict(i) for phrase, found in self.rules.items() if phrase in payload["text"] for i in found]
        return {"items": items, "artifact": self.artifact, "usage": {"prompt_tokens": 10, "completion_tokens": 5}, "model": "fake"}


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Path(self.tmp.name) / "w.sqlite3"
        db.initialize(self.store)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def add_page(self, page_id: str, md: str, kind: str, title: str = "페이지", edited: str = EDITED) -> list[dict]:
        core.record(self.store, scope=SCOPE, kind="notion_record", conversation_key="notion:sec", locator=f"notion:{page_id}",
                    text=parts.md_to_text(md), sent_at=edited)
        built = parts.build_unit_parts(page_id, f"{title} > 본문", md, None)
        parts.save_unit_parts(self.store, SCOPE, "notion", page_id, "sec", built, last_edited=edited)
        found = parts.list_parts(self.store, SCOPE, page_id=page_id)
        for part in found:
            parts.set_part_kind(self.store, part["id"], kind, ME)
        return parts.list_parts(self.store, SCOPE, page_id=page_id)

    def extract(self, extractor) -> int:
        return parts.extract_pending(self.store, SCOPE, extractor, V, budget_tokens=None)

    def rows(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with closing(sqlite3.connect(self.store)) as c:
            c.row_factory = sqlite3.Row
            return c.execute(sql, args).fetchall()

    def items(self, kind: str | None = None) -> list[sqlite3.Row]:
        return self.rows("SELECT * FROM work_items WHERE scope = ?" + (" AND kind = ?" if kind else ""), (SCOPE, kind) if kind else (SCOPE,))

    def run_row(self, key: str) -> sqlite3.Row | None:
        found = self.rows("SELECT * FROM runs WHERE idempotency_key = ?", (key,))
        return found[0] if found else None


DECISION_MD = "# 회의\n- 로그인은 이메일로 정한다\n"


def decision(title: str, quote: str, maturity: str = "agreed", **extra) -> dict:
    return {"kind": "decision", "title": title, "quote": quote, "maturity": maturity,
            **({"agreement_quote": quote} if maturity == "agreed" else {}), **extra}


class PolicyTests(unittest.TestCase):
    def test_policy_table(self) -> None:
        self.assertEqual(apply.policy("unchanged"), "skip")
        self.assertEqual(apply.policy("proposed"), "auto")
        self.assertEqual(apply.policy("supplementary"), "auto")
        self.assertEqual(apply.policy("alternative"), "auto")
        self.assertEqual(apply.policy("resolving"), "auto")
        self.assertEqual(apply.policy("superseding"), "notify")
        self.assertEqual(apply.policy("conflicting", decidable=True, important=True), "related")
        self.assertEqual(apply.policy("conflicting", decidable=False, important=False), "related")
        self.assertEqual(apply.policy("conflicting", decidable=False, important=True), "needs_discussion")
        self.assertEqual(apply.policy("supplementary", protected=True), "needs_discussion")  # 사람이 고친 항목은 덮어쓰지 않는다


class ContractTests(unittest.TestCase):
    TEXT = "로그인은 이메일로 정한다\n최종 로드맵 초안"

    def test_agreed_needs_an_agreement_quote_that_is_in_the_text(self) -> None:
        kept, rejected = parts.check_items([
            {"kind": "decision", "title": "A", "quote": "로그인은 이메일로 정한다", "maturity": "agreed"},
            {"kind": "decision", "title": "B", "quote": "로그인은 이메일로 정한다", "maturity": "agreed", "agreement_quote": "없는 말"},
            {"kind": "decision", "title": "C", "quote": "로그인은 이메일로 정한다", "maturity": "agreed",
             "agreement_quote": "로그인은 이메일로 정한다"},
        ], self.TEXT)
        by_title = {i["title"]: i for i in kept}
        self.assertEqual(by_title["A"]["maturity"], "discussed")  # "최종"·합의 표시만으로는 합의가 아니다
        self.assertEqual(by_title["C"]["maturity"], "agreed")
        self.assertEqual(by_title["B"]["maturity"], "discussed")
        self.assertEqual(rejected, [])

    def test_final_in_the_title_is_not_agreement(self) -> None:
        kept, _ = parts.check_items([{"kind": "decision", "title": "최종 로드맵", "quote": "최종 로드맵 초안"}], self.TEXT)
        self.assertNotEqual(kept[0]["maturity"], "agreed")

    def test_decode_reads_the_old_and_new_shape(self) -> None:
        self.assertEqual(parts.decode_extract('[{"kind": "task"}]'), ([{"kind": "task"}], None))
        items, artifact = parts.decode_extract('{"items": [], "artifact": {"type": "기획", "summary": "x"}}')
        self.assertEqual((items, artifact["type"]), ([], "기획"))


class QuoteMatchTests(unittest.TestCase):
    HAY = parts._squash("**핵심 기능**은 로그인\.  이다 그리고 `결제` 는 나중에")

    def test_markup_the_model_leaves_out_does_not_make_a_valid_quote_invalid(self) -> None:
        found = parts.find_quote("핵심 기능은 로그인. 이다", self.HAY)
        self.assertIn("핵심 기능", found)
        self.assertTrue(found in self.HAY)  # 저장하는 인용은 원문에 적힌 그대로의 구절이다
        self.assertIsNotNone(parts.find_quote("그리고 결제 는 나중에", self.HAY))

    def test_paraphrases_and_joined_fragments_are_still_rejected(self) -> None:
        self.assertIsNone(parts.find_quote("핵심 기능은 회원가입", self.HAY))
        self.assertIsNone(parts.find_quote("핵심 기능 … 나중에", self.HAY))
        self.assertIsNone(parts.find_quote("   ", self.HAY))

    def test_check_items_keeps_the_original_span_as_the_quote(self) -> None:
        kept, rejected = parts.check_items([{"kind": "decision", "title": "A", "quote": "핵심 기능은 로그인"}], "**핵심 기능**은 로그인이다")
        self.assertEqual(rejected, [])
        self.assertEqual(kept[0]["quote"], "핵심 기능**은 로그인")  # 원문에 적힌 그대로(앞쪽 표시는 구절 밖)


class ExploreTests(Base):
    def setUp(self) -> None:
        super().setUp()
        self.add_page("meet-1", DECISION_MD, "meeting", "회의 1")
        self.extract(Scripted({"이메일": [decision("이메일 로그인", "로그인은 이메일로 정한다")]}))
        self.doc = self.add_page("doc-1", "# 기획\n## 로그인\n이메일과 소셜 로그인을 지원한다\n## 소개\n팀 소개 글\n", "document", "기획서")

    def explorer(self, picks: list[str] | None = None, calls: list | None = None):
        def explore_fn(payload: dict) -> dict:
            if calls is not None:
                calls.append(payload)
            ids = [a["area_id"] for s in payload["sources"] for a in s["areas"]]
            return {"read": [{"area_id": a, "reason": "연관"} for a in (picks if picks is not None else ids[:1])] + [{"area_id": "없는-id"}],
                    "model": "fake"}
        return explore_fn

    def test_documents_are_not_interpreted_until_explore_selects_them(self) -> None:
        extractor = Scripted({})
        self.assertEqual(self.extract(extractor), 0)  # 선택 전에는 문서를 보내지 않는다
        self.assertTrue(all("이메일과 소셜" not in c["text"] for c in extractor.calls))
        calls: list = []
        self.assertEqual(explore.run(self.store, SCOPE, self.explorer(calls=calls), V), 1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["recent_meetings"][0]["title"], "이메일 로그인")  # 최근 회의 항목이 출발점
        self.assertNotIn("md", json.dumps(calls[0], ensure_ascii=False))  # 본문은 보내지 않는다
        chosen = parts.selected_ids(self.store, SCOPE)
        self.assertEqual(len(chosen), 1)  # 목록에 없는 id는 버린다
        self.assertEqual(self.extract(extractor), 1)  # 고른 영역만 해석

    def test_a_second_run_hits_the_cache_and_does_not_call_the_llm(self) -> None:
        calls: list = []
        explore.run(self.store, SCOPE, self.explorer(calls=calls), V)
        explore.run(self.store, SCOPE, self.explorer(calls=calls), V)
        self.assertEqual(len(calls), 1)
        extractor = Scripted({})
        self.extract(extractor)
        first = len(extractor.calls)
        self.extract(extractor)
        self.assertEqual(len(extractor.calls), first)  # 해석 캐시: 같은 내용은 다시 부르지 않는다

    def test_a_structure_change_reexplores_but_a_content_change_does_not(self) -> None:
        calls: list = []
        explore.run(self.store, SCOPE, self.explorer(calls=calls), V)
        md2 = "# 기획\n## 로그인\n이메일과 소셜 로그인 그리고 비밀번호 재설정\n## 소개\n팀 소개 글\n"
        self.add_page("doc-1", md2, "document", "기획서")
        self.assertEqual(explore.pending_sources(self.store, SCOPE), [])  # 내용만 바뀜: 다시 탐색하지 않는다
        self.add_page("doc-1", md2 + "## 새 장\n새 내용\n", "document", "기획서")
        self.assertEqual(len(explore.pending_sources(self.store, SCOPE)), 1)  # 구조가 바뀜

    def test_explore_waits_for_unextracted_meetings_and_records_failures(self) -> None:
        self.add_page("meet-2", "# 회의 2\n- 새 안건\n", "meeting", "회의 2")
        calls: list = []
        self.assertEqual(explore.run(self.store, SCOPE, self.explorer(calls=calls), V), 0)
        self.assertEqual(calls, [])
        self.extract(Scripted({}))

        def broken(payload: dict) -> dict:
            return {"read": "not a list"}
        self.assertEqual(explore.run(self.store, SCOPE, broken, V), 0)
        errors = self.rows("SELECT * FROM runs WHERE stage = 'explore' AND status = 'failed'")
        self.assertEqual([e["error_kind"] for e in errors], ["contract"])

    def test_catalog_has_no_bodies(self) -> None:
        source = sections.catalog(self.store, SCOPE)[0]
        self.assertEqual({"source_id", "title", "edited", "fingerprint", "areas"}, set(source))
        self.assertTrue(all(set(a) == {"area_id", "title", "chars", "summary"} for a in source["areas"]))


class ArtifactTests(Base):
    def setUp(self) -> None:
        super().setUp()
        self.add_page("doc-1", "# 기획\n이메일 로그인은 필수라고 정했다\n", "document", "로그인 기획서")
        explore.select_all(self.store, SCOPE, "doc-1", explore.HUMAN)
        self.artifact = {"type": "기획", "summary": "로그인 방식을 정리한 기획서", "maturity": "draft"}
        self.extractor = Scripted({"필수": [{"kind": "requirement", "title": "이메일 로그인 필수", "quote": "이메일 로그인은 필수",
                                             "maturity": "draft"},
                                            decision("이메일 로그인 확정", "이메일 로그인은 필수라고 정했다")]}, self.artifact)

    def test_artifact_item_with_source_link_and_derived_from(self) -> None:
        self.extract(self.extractor)
        part = parts.list_parts(self.store, SCOPE, page_id="doc-1")[0]
        apply.apply_part(self.store, SCOPE, part["id"], ME, V)
        stats = lineage.sync_artifacts(self.store, SCOPE, V)
        self.assertEqual((stats["created"], stats["linked"]), (1, 2))
        artifact = self.items("artifact")[0]
        self.assertEqual(artifact["title"], "로그인 기획서")
        self.assertIn("로그인 방식을 정리한 기획서", artifact["statement_md"])
        self.assertIn("https://www.notion.so/doc1", artifact["statement_md"])
        attributes = json.loads(artifact["attributes_json"])
        self.assertEqual((attributes["artifact_type"], attributes["page_id"]), ("기획", "doc-1"))
        links = self.rows("SELECT * FROM work_links WHERE type = 'DERIVED_FROM' AND target_id = ?", (artifact["id"],))
        self.assertEqual(len(links), 2)
        by_title = {i["title"]: json.loads(i["attributes_json"]) for i in self.items() if i["kind"] != "artifact"}
        self.assertEqual(by_title["이메일 로그인 필수"]["maturity"], "draft")
        self.assertEqual(by_title["이메일 로그인 확정"]["maturity"], "agreed")
        self.assertEqual([i["status"] for i in self.items() if i["status"] == "needs_discussion"], [])  # 문서는 자동으로 확인 대기가 아니다
        again = lineage.sync_artifacts(self.store, SCOPE, V)
        self.assertEqual((again["created"], again["updated"], again["linked"]), (0, 0, 0))

    def test_a_human_edited_artifact_is_not_overwritten_and_a_cancelled_one_not_recreated(self) -> None:
        self.extract(self.extractor)
        lineage.sync_artifacts(self.store, SCOPE, V)
        artifact = self.items("artifact")[0]
        with db.write(self.store) as c:
            core._write_item(c, artifact["id"], {"statement_md": "사람이 고친 설명"}, "구조 화면에서 고침", "송지원", db.now())
        self.extractor.artifact = {"type": "기획", "summary": "완전히 다른 요약", "maturity": "draft"}
        self.add_page("doc-1", "# 기획\n이메일 로그인은 필수라고 정했다\n추가 문장\n", "document", "로그인 기획서")
        explore.select_all(self.store, SCOPE, "doc-1", explore.HUMAN)
        self.extract(self.extractor)
        stats = lineage.sync_artifacts(self.store, SCOPE, V)
        self.assertEqual(stats["protected"], 1)
        self.assertEqual(self.items("artifact")[0]["statement_md"], "사람이 고친 설명")
        with db.write(self.store) as c:
            core._write_item(c, artifact["id"], {"status": "cancelled"}, "구조 화면에서 지움", "송지원", db.now())
        self.assertEqual(lineage.sync_artifacts(self.store, SCOPE, V)["created"], 0)  # 사람이 지운 것은 다시 만들지 않는다
        self.assertEqual(len(self.items("artifact")), 1)


class RelationTests(Base):
    def setUp(self) -> None:
        super().setUp()
        self.add_page("meet-1", "# 회의 1\n- 로그인은 이메일로 정한다\n- 결제 수단은 아직 미정\n", "meeting", "회의 1")
        self.extract(Scripted({"로그인": [decision("이메일 로그인", "로그인은 이메일로 정한다")],
                               "결제": [{"kind": "open", "title": "결제 수단 미정", "quote": "결제 수단은 아직 미정", "maturity": "discussed"}]}))
        self.apply_page("meet-1")

    def apply_page(self, page_id: str, judge=None) -> dict:
        return apply.apply_page(self.store, SCOPE, page_id, ME, V, judge=judge)

    def judge_for(self, change_type: str, kind: str, **extra):
        def judge(item: dict, related: list[dict]) -> dict:
            target = next(r for r in related if r["kind"] == kind)
            return {"change_type": change_type, "target_id": target["id"], "affected_task_ids": [], "rationale": "테스트", **extra}
        return judge

    def second_page(self, line: str, item: dict) -> None:
        self.add_page("meet-2", f"# 회의 2\n- {line}\n", "meeting", "회의 2")
        self.extract(Scripted({line[:6]: [item]}))

    def test_alternative_keeps_the_decision_and_revert_removes_the_new_item(self) -> None:
        self.second_page("로그인은 소셜로 하면 어떨까", {"kind": "decision", "title": "소셜 로그인 안", "quote": "로그인은 소셜로 하면 어떨까",
                                                         "maturity": "draft"})
        summary = self.apply_page("meet-2", self.judge_for("alternative", "decision"))
        self.assertEqual((summary["applied"], summary["needs_discussion"]), (1, 0))
        base = next(i for i in self.items("decision") if i["title"] == "이메일 로그인")
        alt = next(i for i in self.items("decision") if i["title"] == "소셜 로그인 안")
        self.assertEqual(base["status"], "active")  # 기존 결정은 그대로
        link = self.rows("SELECT * FROM work_links WHERE type = 'ALTERNATIVE_TO'")[0]
        self.assertEqual((link["source_id"], link["target_id"]), (alt["id"], base["id"]))
        candidate = self.rows("SELECT id FROM change_candidates WHERE applied_item_id = ?", (alt["id"],))[0]["id"]
        apply.revert(self.store, candidate, ME)
        self.assertEqual(self.rows("SELECT status FROM work_items WHERE id = ?", (alt["id"],))[0]["status"], "reverted")
        self.assertEqual(self.rows("SELECT * FROM work_links WHERE type = 'ALTERNATIVE_TO'"), [])
        self.assertEqual(self.rows("SELECT status FROM work_items WHERE id = ?", (base["id"],))[0]["status"], "active")

    def test_resolving_closes_the_open_item_and_revert_reopens_it(self) -> None:
        self.second_page("결제는 카드로 하기로 정했다", {"kind": "decision", "title": "카드 결제", "quote": "결제는 카드로 하기로 정했다",
                                                        "maturity": "agreed", "agreement_quote": "결제는 카드로 하기로 정했다"})
        self.apply_page("meet-2", self.judge_for("resolving", "open"))
        open_item = next(i for i in self.items("open"))
        self.assertEqual(open_item["status"], "resolved")
        self.assertEqual(len(self.rows("SELECT 1 FROM work_links WHERE type = 'RESOLVES'")), 1)
        new = next(i for i in self.items("decision") if i["title"] == "카드 결제")
        candidate = self.rows("SELECT id FROM change_candidates WHERE applied_item_id = ?", (new["id"],))[0]["id"]
        apply.revert(self.store, candidate, ME)
        self.assertEqual(self.rows("SELECT status FROM work_items WHERE id = ?", (open_item["id"],))[0]["status"], "active")  # 미정은 원래대로

    def test_superseding_needs_agreement_otherwise_it_becomes_an_alternative(self) -> None:
        draft = {"kind": "decision", "title": "소셜 로그인으로 변경", "quote": "로그인을 소셜로 바꾼다", "maturity": "draft"}
        self.second_page("로그인을 소셜로 바꾼다", draft)
        self.apply_page("meet-2", self.judge_for("superseding", "decision"))
        self.assertEqual(self.rows("SELECT status FROM work_items WHERE title = '이메일 로그인'")[0]["status"], "active")
        self.assertEqual(len(self.rows("SELECT 1 FROM work_links WHERE type = 'ALTERNATIVE_TO'")), 1)

    def test_agreed_supersede_replaces_and_is_revertable(self) -> None:
        agreed = decision("소셜 로그인으로 변경", "로그인을 소셜로 바꾸기로 했다")
        self.second_page("로그인을 소셜로 바꾸기로 했다", agreed)
        summary = self.apply_page("meet-2", self.judge_for("superseding", "decision"))
        self.assertEqual(len(summary["superseded"]), 1)
        self.assertEqual(self.rows("SELECT status FROM work_items WHERE title = '이메일 로그인'")[0]["status"], "superseded")
        apply.revert(self.store, summary["superseded"][0]["candidate_id"], ME)
        self.assertEqual(self.rows("SELECT status FROM work_items WHERE title = '이메일 로그인'")[0]["status"], "active")

    def test_only_an_undecidable_important_conflict_needs_discussion(self) -> None:
        base = next(i for i in self.items("decision"))
        task = {"kind": "task", "title": "로그인 구현", "quote": "로그인 구현", "maturity": "discussed"}
        with db.write(self.store) as c:
            task_id = core._create_item(c, SCOPE, "task", {"title": "로그인 구현", "statement": "로그인 구현"}, "시드", ME, db.now())
            core._link(c, base["id"], task_id, core.LINK_AFFECTS, None, db.now())
        self.second_page("로그인은 전화번호로 정한다", decision("전화번호 로그인", "로그인은 전화번호로 정한다"))
        self.apply_page("meet-2", self.judge_for("conflicting", "decision", decidable=False))
        new = next(i for i in self.items("decision") if i["title"] == "전화번호 로그인")
        self.assertEqual(new["status"], "needs_discussion")
        self.assertEqual(self.rows("SELECT status FROM work_items WHERE id = ?", (base["id"],))[0]["status"], "active")
        del task

    def test_a_decidable_conflict_only_links_both_items(self) -> None:
        self.second_page("로그인은 전화번호로 정한다", decision("전화번호 로그인", "로그인은 전화번호로 정한다"))
        summary = self.apply_page("meet-2", self.judge_for("conflicting", "decision", decidable=True))
        self.assertEqual(summary["needs_discussion"], 0)
        self.assertEqual(len(self.rows("SELECT 1 FROM work_links WHERE type = 'RELATED_TO'")), 1)
        self.assertEqual(len(self.items("decision")), 2)  # 둘 다 살아 있다

    def test_a_human_edited_target_is_not_overwritten(self) -> None:
        base = next(i for i in self.items("decision"))
        with db.write(self.store) as c:
            core._write_item(c, base["id"], {"statement_md": "사람이 고친 결정"}, "구조 화면에서 고침", "송지원", db.now())
        self.second_page("로그인은 이메일 인증을 더한다", {"kind": "decision", "title": "이메일 인증 추가", "quote": "로그인은 이메일 인증을 더한다",
                                                          "maturity": "discussed"})
        self.apply_page("meet-2", self.judge_for("supplementary", "decision"))
        self.assertEqual(self.rows("SELECT statement_md FROM work_items WHERE id = ?", (base["id"],))[0]["statement_md"], "사람이 고친 결정")
        self.assertEqual(next(i for i in self.items("decision") if i["title"] == "이메일 인증 추가")["status"], "needs_discussion")

    def test_judge_failure_is_a_technical_error_not_a_discussion(self) -> None:
        self.second_page("로그인은 소셜로 하면 어떨까", {"kind": "decision", "title": "소셜 로그인 안", "quote": "로그인은 소셜로 하면 어떨까",
                                                         "maturity": "draft"})

        def bad_judge(item: dict, related: list[dict]) -> dict:
            return {"change_type": "alternative"}  # target_id 없음: 계약 위반

        part = apply.meeting_parts(self.store, SCOPE, "meet-2")[0]
        key = runs.key_for("reconcile", SCOPE, part["id"])
        summary = self.apply_page("meet-2", bad_judge)
        self.assertEqual((summary["errors"], summary["needs_discussion"]), (1, 0))
        self.assertEqual([i["title"] for i in self.items("decision")], ["이메일 로그인"])  # 항목을 만들지 않았다
        self.assertEqual(self.run_row(key)["error_kind"], "contract")
        self.assertNotEqual(parts.list_parts(self.store, SCOPE, page_id="meet-2")[0]["state"], "applied")
        for _ in range(runs.MAX_ATTEMPTS):
            self.apply_page("meet-2", bad_judge)
        self.assertTrue(runs.exhausted(self.store, key))
        self.assertEqual(len(runs.stuck(self.store, SCOPE)), 1)
        calls: list = []
        self.apply_page("meet-2", lambda *a: calls.append(a) or {})  # 멈춘 부분은 다시 부르지 않는다
        self.assertEqual(calls, [])
        self.assertEqual(runs.reprocess(self.store, SCOPE, "contract"), 1)
        summary = self.apply_page("meet-2", self.judge_for("alternative", "decision"))
        self.assertEqual(summary["applied"], 1)
        self.assertEqual(self.run_row(key)["status"], "succeeded")
        self.assertEqual(runs.stuck(self.store, SCOPE), [])

    def test_a_busy_judge_is_not_counted_as_an_attempt(self) -> None:
        self.second_page("로그인은 소셜로 하면 어떨까", {"kind": "decision", "title": "소셜 로그인 안", "quote": "로그인은 소셜로 하면 어떨까",
                                                         "maturity": "draft"})

        def busy_judge(item: dict, related: list[dict]) -> dict:
            raise Busy("rate limit 429")

        part = apply.meeting_parts(self.store, SCOPE, "meet-2")[0]
        for _ in range(5):
            self.apply_page("meet-2", busy_judge)
        row = self.run_row(runs.key_for("reconcile", SCOPE, part["id"]))
        self.assertEqual((row["error_kind"], row["attempts"]), ("busy", 0))
        self.assertFalse(runs.exhausted(self.store, row["idempotency_key"]))


class FailureRecoveryTests(Base):
    def setUp(self) -> None:
        super().setUp()
        self.page = self.add_page("meet-1", DECISION_MD, "meeting", "회의 1")
        self.key = parts.interpret_key(SCOPE, self.page[0], V)

    def test_quote_mismatch_is_recorded_then_retried_with_feedback_and_closed(self) -> None:
        answers = [[decision("이메일 로그인", "원문에 없는 문장")], [decision("이메일 로그인", "로그인은 이메일로 정한다")]]
        payloads: list[dict] = []

        def extractor(payload: dict) -> dict:
            payloads.append(payload)
            return {"items": answers[min(len(payloads) - 1, 1)], "usage": {}, "model": "fake"}

        self.extract(extractor)
        row = self.run_row(self.key)
        self.assertEqual((row["status"], row["error_kind"], row["attempts"]), ("failed", "quote_mismatch", 1))
        self.assertEqual(self.items(), [])  # 잘못된 인용은 항목이 되지 않는다
        self.extract(extractor)
        self.assertIn("원문에 없는 문장", payloads[1]["feedback"])  # 무엇이 틀렸는지 알려 준다
        self.assertEqual(self.run_row(self.key)["status"], "succeeded")
        self.assertEqual(len(parts.meeting_items(self.store, SCOPE, V)), 1)

    def test_a_contract_violation_stops_after_three_and_reprocess_reopens_it(self) -> None:
        calls = []

        def bad(payload: dict) -> dict:
            calls.append(1)
            return {"items": "not a list"}

        for _ in range(5):
            self.extract(bad)
        self.assertEqual(len(calls), runs.MAX_ATTEMPTS)  # 3번 뒤에는 다시 부르지 않는다
        row = self.run_row(self.key)
        self.assertEqual((row["error_kind"], row["attempts"]), ("contract", 3))
        self.assertEqual(self.items(), [])
        self.assertEqual(runs.reprocess(self.store, SCOPE), 1)
        self.extract(Scripted({"이메일": [decision("이메일 로그인", "로그인은 이메일로 정한다")]}))
        self.assertEqual(self.run_row(self.key)["status"], "succeeded")

    def test_a_busy_extractor_stops_the_cycle_without_counting(self) -> None:
        def busy(payload: dict) -> dict:
            raise Busy("429")

        self.extract(busy)
        self.assertIsNone(self.run_row(self.key) and self.run_row(self.key)["attempts"] or None)

    def test_the_admin_summary_line_shows_only_for_admins(self) -> None:
        def bad(payload: dict) -> dict:
            return {"items": "x"}

        for _ in range(3):
            self.extract(bad)
        with patch.object(worklog_bridge, "WORKLOG_STORE", self.store):
            admin_lines, _ = confirm_flow.main_card_alerts(SCOPE, admin=True)
            member_lines, _ = confirm_flow.main_card_alerts(SCOPE, admin=False)
        self.assertTrue([line for line in admin_lines if "멈춘" in line and "1건" in line])
        self.assertFalse([line for line in member_lines if "멈춘" in line])


class ItemLimitTests(Base):
    def test_an_overfull_piece_is_split_so_no_valid_item_is_lost(self) -> None:
        lines = [f"- 요구사항 {i}번을 만든다" for i in range(parts.MAX_ITEMS_PER_PART * 2 + 6)]
        self.add_page("doc-1", "# 문서" + chr(10) + chr(10).join(lines) + chr(10), "document", "문서")
        explore.select_all(self.store, SCOPE, "doc-1", explore.HUMAN)
        calls: list[int] = []

        def extractor(payload: dict) -> dict:
            found = [line for line in payload["text"].split(chr(10)) if "번을 만든다" in line]
            calls.append(len(found))
            return {"items": [{"kind": "requirement", "title": line[:20], "quote": line} for line in found], "usage": {}, "model": "fake"}

        self.extract(extractor)
        part = parts.list_parts(self.store, SCOPE, page_id="doc-1")[0]
        kept = parts.decode_extract(self.rows("SELECT items_json FROM part_extracts")[0]["items_json"])[0]
        self.assertEqual(len(kept), len(lines))  # 하나도 버리지 않았다
        self.assertEqual(self.rows("SELECT dropped FROM part_extracts")[0]["dropped"], 0)
        self.assertGreater(len(calls), 1)
        self.assertLessEqual(calls[-1], parts.MAX_ITEMS_PER_PART)  # 계속 나눠 상한 안에서 끝낸다
        before = len(calls)
        self.extract(extractor)
        self.assertEqual(len(calls), before)  # 완성된 부분은 캐시
        del part

    def test_only_an_unsplittable_piece_is_truncated_and_counted(self) -> None:
        self.add_page("meet-1", "# 회의" + chr(10) + "- 로그인은 이메일로 정한다" + chr(10), "meeting", "회의 1")
        many = [{"kind": "open", "title": f"질문 {i}", "quote": "로그인은 이메일로 정한다"} for i in range(parts.MAX_ITEMS_PER_PART + 5)]
        self.extract(lambda payload: {"items": many, "usage": {}, "model": "fake"})
        self.assertLessEqual(len(parts.meeting_items(self.store, SCOPE, V)), parts.MAX_ITEMS_PER_PART)
        self.assertGreater(self.rows("SELECT dropped FROM part_extracts")[0]["dropped"], 0)
        self.assertEqual(self.rows("SELECT * FROM runs WHERE status = 'failed'"), [])


class LongPartTests(Base):
    def test_a_long_part_resumes_after_a_busy_limit_without_repeating_pieces(self) -> None:
        body = chr(10).join(f"- 항목 {i}번 문장입니다" for i in range(900))
        self.add_page("doc-1", f"# 긴 문서{chr(10)}{body}{chr(10)}", "document", "긴 문서")
        explore.select_all(self.store, SCOPE, "doc-1", explore.HUMAN)
        seen: list[str] = []
        state = {"fail_on": 2}

        def extractor(payload: dict) -> dict:
            seen.append(payload["text"][:12])
            if len(seen) == state["fail_on"]:
                raise Busy("429")
            return {"items": [], "artifact": {"type": "기타", "summary": "긴 문서", "maturity": "draft"}, "usage": {}, "model": "fake"}

        self.extract(extractor)
        first = len(seen)
        self.assertEqual(first, 2)  # 둘째 조각에서 한도
        self.assertEqual(parts.pending_count(self.store, SCOPE, V), 1)
        state["fail_on"] = 0
        self.extract(extractor)
        pieces = len(parts._chunks(parts.md_to_text(parts.list_parts(self.store, SCOPE, page_id="doc-1")[0]["md"]), parts.EXTRACT_BUDGET_TOKENS))
        self.assertGreater(pieces, 2)
        self.assertEqual(len(seen) - first, pieces - 1)  # 첫 조각은 다시 보내지 않는다
        self.assertEqual(parts.pending_count(self.store, SCOPE, V), 0)


class RetryResumeTests(Base):
    def test_a_retry_with_feedback_resumes_pieces_after_a_busy_limit(self) -> None:
        body = chr(10).join(f"- 항목 {i}번 문장입니다" for i in range(900))
        self.add_page("doc-1", "# 긴 문서" + chr(10) + body + chr(10), "document", "긴 문서")
        explore.select_all(self.store, SCOPE, "doc-1", explore.HUMAN)
        part = parts.list_parts(self.store, SCOPE, page_id="doc-1")[0]
        key = parts.interpret_key(SCOPE, part, V)
        runs.report(self.store, key, "interpret", "quote_mismatch", "원문에 없는 인용 1건", result={"rejected": [{"title": "t", "quote": "q"}]})
        seen: list[str] = []
        state = {"fail_on": 2}

        def extractor(payload: dict) -> dict:
            seen.append(payload["text"][:12])
            self.assertIn("feedback", payload)
            if len(seen) == state["fail_on"]:
                raise Busy("429")
            return {"items": [], "usage": {}, "model": "fake"}

        self.extract(extractor)
        first = len(seen)
        state["fail_on"] = 0
        self.extract(extractor)
        pieces = len(parts._chunks(parts.md_to_text(part["md"]), parts.EXTRACT_BUDGET_TOKENS))
        self.assertEqual(len(seen) - first, pieces - (first - 1))  # 한도 전에 끝낸 조각은 다시 묻지 않는다
        self.assertEqual(self.run_row(key)["status"], "succeeded")


class MoveTests(Base):
    def test_a_moved_part_is_followed_by_quote_and_a_missing_one_is_flagged(self) -> None:
        self.add_page("meet-1", "# 회의\n- 로그인은 이메일로 정한다\n", "meeting", "회의")
        self.extract(Scripted({"이메일": [decision("이메일 로그인", "로그인은 이메일로 정한다")]}))
        apply.apply_page(self.store, SCOPE, "meet-1", ME, V)
        old_part = parts.list_parts(self.store, SCOPE, page_id="meet-1")[0]
        item = self.items("decision")[0]
        # 원문이 다른 페이지로 옮겨졌다(부분 ID가 달라졌다)
        with db.write(self.store) as c:
            c.execute("UPDATE source_parts SET status = 'gone' WHERE id = ?", (old_part["id"],))
        moved = self.add_page("meet-9", "# 회의\n- 로그인은   이메일로 정한다\n- 그 밖의 말\n", "meeting", "옮긴 회의")
        self.assertEqual(lineage.reconcile_gone(self.store, SCOPE), {"moved": 1, "missing": 0})
        attributes = json.loads(self.rows("SELECT attributes_json FROM work_items WHERE id = ?", (item["id"],))[0]["attributes_json"])
        self.assertEqual(attributes["part_ids"], [moved[0]["id"]])
        self.assertNotIn("source_missing", attributes)
        self.assertEqual(apply.gone_confirmed_parts(self.store, SCOPE), [])  # 옮겨진 부분은 삭제를 묻지 않는다
        self.assertEqual(self.rows("SELECT state FROM source_parts WHERE id = ?", (old_part["id"],))[0]["state"], "moved")

    def test_a_vanished_source_is_flagged_not_deleted(self) -> None:
        self.add_page("meet-1", "# 회의\n- 로그인은 이메일로 정한다\n", "meeting", "회의")
        self.extract(Scripted({"이메일": [decision("이메일 로그인", "로그인은 이메일로 정한다")]}))
        apply.apply_page(self.store, SCOPE, "meet-1", ME, V)
        part = parts.list_parts(self.store, SCOPE, page_id="meet-1")[0]
        with db.write(self.store) as c:
            c.execute("UPDATE source_parts SET status = 'gone' WHERE id = ?", (part["id"],))
        self.assertEqual(lineage.reconcile_gone(self.store, SCOPE), {"moved": 0, "missing": 1})
        self.assertEqual(lineage.reconcile_gone(self.store, SCOPE), {"moved": 0, "missing": 0})  # 두 번 표시하지 않는다
        item = self.items("decision")[0]
        self.assertTrue(json.loads(item["attributes_json"])["source_missing"])
        self.assertEqual(item["status"], "active")
        self.assertEqual(len(apply.gone_confirmed_parts(self.store, SCOPE)), 1)  # 지울지는 권한자가 정한다


class UploadTypeTests(Base):
    def test_a_non_meeting_type_becomes_a_selected_document(self) -> None:
        page_id = meeting_inputs.save_note(self.store, SCOPE, "# 조사 결과\n사용자 10명을 인터뷰했다\n", "조사 메모", "teams", ME, "조사")
        found = parts.list_parts(self.store, SCOPE, page_id=page_id)
        self.assertTrue(all(p["kind"] == "document" and p["kind_note"] == "조사" for p in found))
        self.assertEqual(parts.selected_ids(self.store, SCOPE), {p["id"] for p in found})
        self.assertEqual({p["id"] for p in parts.interpretable(self.store, SCOPE)}, {p["id"] for p in found})
        extractor = Scripted({"인터뷰": []}, {"type": "기타", "summary": "인터뷰 조사 메모", "maturity": "discussed"})
        self.extract(extractor)
        lineage.sync_artifacts(self.store, SCOPE, V)
        self.assertEqual(json.loads(self.items("artifact")[0]["attributes_json"])["artifact_type"], "조사")  # 올릴 때 고른 종류가 먼저

    def test_the_default_type_is_a_meeting_and_unknown_types_are_refused(self) -> None:
        page_id = meeting_inputs.save_note(self.store, SCOPE, "# 회의\n오늘 정한 것이 있다\n", "회의", "teams", ME)
        self.assertEqual({p["kind"] for p in parts.list_parts(self.store, SCOPE, page_id=page_id)}, {"meeting"})
        with self.assertRaises(db.ValidationError):
            meeting_inputs.save_note(self.store, SCOPE, "# 글\n내용\n", "글", "teams", ME, "없는 종류")


class BridgeContractTests(unittest.TestCase):
    def test_a_non_json_answer_is_retried_once_then_raises_a_contract_error(self) -> None:
        class Resp:
            status_code = 200
            headers: dict = {}

            def json(self) -> dict:
                return {"choices": [{"message": {"content": "JSON이 아니다"}}], "usage": {"total_tokens": 1}}

            def raise_for_status(self) -> None:
                return None

        class Client:
            posts: list = []

            def __init__(self, *a, **k) -> None:
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a) -> None:
                return None

            def post(self, url, **kwargs):
                Client.posts.append(kwargs["json"]["messages"][1]["content"])
                return Resp()

        provider = type("P", (), {"name": "fake", "url": "http://x", "key": "k", "model": "m"})()
        with patch.object(worklog_bridge.httpx, "Client", Client), \
                patch.object(worklog_bridge.llm_queue, "available", lambda priority=None: [provider]), \
                patch.object(worklog_bridge.llm_queue, "check", lambda *a, **k: None), \
                patch.object(worklog_bridge.llm_queue, "record", lambda *a, **k: None), \
                patch.object(worklog_bridge.metrics, "count_groq", lambda usage: None):
            with self.assertRaises(ContractError):
                worklog_bridge._llm_call("시스템", {"a": 1})
        self.assertEqual(len(Client.posts), 2)  # 한 번 더 부탁했다
        self.assertIn("JSON 객체 하나만", Client.posts[1])


if __name__ == "__main__":
    unittest.main()
