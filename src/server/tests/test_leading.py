"""v2.1 Phase 4: 프로젝트 리딩(메뉴 1·2). 문서: docs/implementation/v2.1/plan.md Phase 4. 모든 데이터는 지어낸 것이다."""

from __future__ import annotations

import isolation  # noqa: F401,I001  실제 저장소·키를 쓰지 않게(맨 먼저)

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main  # noqa: E402
import worklog_bridge  # noqa: E402
from worklog import core, db, explore, leading, lineage, parts, sections  # noqa: E402
from worklog.db import ContractError  # noqa: E402

SCOPE = "team:T1"
ME = "송지원"
V = "p4-test"


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Path(self.tmp.name) / "w.sqlite3"
        db.initialize(self.store)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def add_page(self, page_id: str, page_path: str, kind: str, date: str, md: str = "# 본문\n내용\n") -> str:
        """실제 경로 구조(루트 > … > 페이지 제목 > 부분 제목)로 페이지 하나를 저장하고 첫 부분 ID를 돌려준다."""
        core.record(self.store, scope=SCOPE, kind="notion_record", conversation_key="notion:sec", locator=f"notion:{page_id}",
                    text=f"날짜: {date}\n" + parts.md_to_text(md), sent_at=f"{date}T00:00:00Z")
        built = parts.build_unit_parts(page_id, page_path, md, None)
        parts.save_unit_parts(self.store, SCOPE, "notion", page_id, "sec", built, last_edited=f"{date}T00:00:00Z")
        found = parts.list_parts(self.store, SCOPE, page_id=page_id)
        for part in found:
            parts.set_part_kind(self.store, part["id"], kind, ME)
        return found[0]["id"]

    def make(self, kind: str, title: str, part_id: str | None, maturity: str | None = None, assignee: str | None = None,
             refs: list[dict] | None = None, statement: str | None = None, status: str | None = None, extra: dict | None = None) -> str:
        proposal: dict = {"title": title, "statement": statement or f"{title} 설명"}
        if maturity:
            proposal["maturity"] = maturity
        if assignee:
            proposal["assignee"] = assignee
        if refs:
            proposal["github_refs"] = refs
        if extra:
            proposal.update(extra)
        with db.write(self.store) as c:
            item_id = core._create_item(c, SCOPE, kind, proposal, "시드", ME, db.now())
            if part_id:
                attributes = json.loads(c.execute("SELECT attributes_json FROM work_items WHERE id = ?", (item_id,)).fetchone()[0])
                attributes["part_ids"] = [part_id]
                core._write_item(c, item_id, {"attributes_json": attributes}, "출처 연결", ME, db.now())
            if status:
                core._write_item(c, item_id, {"status": status}, "상태", ME, db.now())
        return item_id


class SourceNameTests(Base):
    def test_page_title_is_the_last_segment_not_the_root_name(self) -> None:
        part_id = self.add_page("doc-1", "루트 > 문서 > 최종 기획서", "document", "2026-10-02")
        found = parts.list_parts(self.store, SCOPE, page_id="doc-1")
        self.assertEqual(sections.page_title(found), "최종 기획서")
        self.assertEqual(sections.page_path(found), "루트 > 문서 > 최종 기획서")
        self.assertEqual(sections.catalog(self.store, SCOPE, include_all=True)[0]["title"], "최종 기획서")
        del part_id

    def test_artifact_titles_use_the_page_title_and_an_old_root_title_is_corrected(self) -> None:
        a = self.add_page("doc-1", "루트 > 기획 > 로그인 기획", "document", "2026-10-01")
        b = self.add_page("doc-2", "루트 > 기획 > 결제 기획", "document", "2026-10-02")
        for page_id in ("doc-1", "doc-2"):
            explore.select_all(self.store, SCOPE, page_id, explore.HUMAN)
        extractor = lambda payload: {"items": [], "artifact": {"type": "기획", "summary": "요약 " + payload["path"][-6:], "maturity": "draft"},  # noqa: E731
                                     "usage": {}, "model": "fake"}
        parts.extract_pending(self.store, SCOPE, extractor, V, budget_tokens=None)
        lineage.sync_artifacts(self.store, SCOPE, V)
        titles = sorted(r["title"] for r in self._rows("SELECT title FROM work_items WHERE kind = 'artifact'"))
        self.assertEqual(titles, ["결제 기획", "로그인 기획"])  # 루트 이름이 아니라 페이지 제목이라 서로 다르다
        # 예전 버전이 루트 이름으로 붙여 둔 제목은 다시 해석할 때 페이지 제목으로 바로잡는다
        with db.write(self.store) as c:
            row = c.execute("SELECT id FROM work_items WHERE title = '로그인 기획'").fetchone()
            core._write_item(c, row["id"], {"title": "루트", "normalized_title": db.normalize_text("루트")}, "예전 제목", "자동(산출물 해석)", db.now())
        lineage.sync_artifacts(self.store, SCOPE, V)
        self.assertIn("로그인 기획", [r["title"] for r in self._rows("SELECT title FROM work_items WHERE kind = 'artifact'")])
        del a, b

    def _rows(self, sql: str) -> list:
        import sqlite3
        from contextlib import closing

        with closing(sqlite3.connect(self.store)) as c:
            c.row_factory = sqlite3.Row
            return c.execute(sql).fetchall()

    def test_duplicate_titles_are_told_apart_by_parent_folder_then_by_id_tail(self) -> None:
        pages = [{"page_id": "aaaa-0001", "part_id": "p1", "title": "최종 기획서", "page_path": "루트 > 기획 > 최종 기획서"},
                 {"page_id": "bbbb-0002", "part_id": "p2", "title": "최종 기획서", "page_path": "루트 > 조사 > 최종 기획서"},
                 {"page_id": "cccc-0003", "part_id": "p3", "title": "회의 메모", "page_path": "루트 > 회의 > 회의 메모"}]
        labels = leading.label_pages(pages)
        self.assertEqual(labels["aaaa-0001"], "기획 > 최종 기획서")
        self.assertEqual(labels["bbbb-0002"], "조사 > 최종 기획서")
        self.assertEqual(labels["cccc-0003"], "회의 메모")  # 겹치지 않으면 제목 그대로
        same_folder = [dict(pages[0]), dict(pages[0], page_id="dddd-0004", part_id="p4")]
        tail = leading.label_pages(same_folder)
        self.assertEqual(len(set(tail.values())), 2)  # 경로까지 같아도 ID 꼬리로 구분한다
        self.assertTrue(all("#" in v for v in tail.values()))


class ProjectDocsTests(Base):
    def test_the_newest_document_is_never_pushed_out_by_older_ones(self) -> None:
        for n in range(30):
            pid = self.add_page(f"old-{n}", f"루트 > 기획 > 옛 기획 {n}", "document", f"2026-09-{(n % 28) + 1:02d}")
            self.make("artifact", f"옛 기획 {n}", pid, statement="- 본문: " + "긴 요약 " * 80, extra={"artifact_type": "기획"})
        newest = self.add_page("new", "루트 > 기획 > 최종 기획서", "document", "2026-10-20")
        self.make("artifact", "최종 기획서", newest, statement="- 본문: 최신 기획 내용", extra={"artifact_type": "기획", "maturity": "agreed"})
        found = leading.project_docs(self.store, SCOPE, budget_tokens=1200)
        self.assertEqual(found["docs"][0]["title"], "최종 기획서")  # 최근 순이라 맨 앞
        self.assertEqual(found["docs"][0]["date"], "2026-10-20")
        self.assertGreater(found["omitted"], 0)  # 예산 밖으로 밀리는 것은 오래된 쪽
        self.assertLessEqual(len(found["docs"]), 30)
        self.assertIsNone(leading.project_input(Path(self.tmp.name) / "empty.sqlite3", SCOPE, "팀 소개"))  # 문서가 없으면 예전 방식

    def test_render_project_checks_every_source_id_and_builds_the_source_list(self) -> None:
        pid = self.add_page("doc-1", "루트 > 기획 > 최종 기획서", "document", "2026-10-02")
        self.make("artifact", "최종 기획서", pid, statement="- 본문: 로그인 기획", extra={"artifact_type": "기획"})
        built = leading.project_input(self.store, SCOPE, "우리 팀은 알파팀")
        answer = {"paragraphs": [
            {"text": "로그인 방식을 기획했어요.", "sources": ["s1", "없는-id"]},
            {"text": "근거 id가 입력에 없는 문단", "sources": ["s9"]},
            {"text": "출처가 비어 있는 문단", "sources": []},
            {"text": "이 기능은 검증 완료됐어요.", "sources": ["s1"]},
            {"text": "팀 소개 문단", "sources": ["t"]}]}
        text = leading.render_project(answer, built["refs"])
        self.assertIn("로그인 방식을 기획했어요. [1]", text)
        self.assertIn("팀 소개 문단 [2]", text)
        for dropped in ("근거 id가 입력에 없는", "출처가 비어", "검증 완료"):
            self.assertNotIn(dropped, text)
        self.assertIn("[1] 최종 기획서(2026.10.02) — https://www.notion.so/doc1", text)
        self.assertIn("[2] 팀 정보 페이지", text)
        with self.assertRaises(ContractError):
            leading.render_project({"paragraphs": [{"text": "x", "sources": ["s9"]}]}, built["refs"])
        with self.assertRaises(ContractError):
            leading.render_project({"paragraphs": "no"}, built["refs"])


class BriefingTests(Base):
    def setUp(self) -> None:
        super().setUp()
        self.meet = self.add_page("meet-1", "루트 > 회의록 > 2026-10-05 회의", "meeting", "2026-10-05")
        self.doc = self.add_page("doc-1", "루트 > 기획 > 최종 기획서", "document", "2026-10-02")
        self.agreed = self.make("decision", "이메일 로그인 확정", self.meet, maturity="agreed")
        self.wired = self.make("requirement", "알림 기능", self.doc, maturity="agreed", refs=[{"type": "issue", "number": 7, "basis": "inferred"}])
        self.draft = self.make("requirement", "소셜 로그인 가안", self.doc, maturity="draft")
        self.alt = self.make("decision", "소셜 로그인 안", self.doc, maturity="draft")
        self.legacy = self.make("decision", "예전에 반영한 결정", self.meet)  # 성숙도 기록 전
        self.opened = self.make("open", "결제 수단 미정", self.meet, maturity="discussed")
        self.task = self.make("task", "로그인 API 만들기", self.meet, assignee="김팀원")
        self.free_task = self.make("task", "인터뷰 질문지", self.meet)
        self.orphan = self.make("decision", "출처 없는 결정", None, maturity="agreed")
        with db.write(self.store) as c:
            core._link(c, self.alt, self.agreed, core.LINK_ALTERNATIVE_TO, None, db.now())

    def built(self, states=None, touched=None, task_states=None):
        return leading.briefing_items(self.store, SCOPE, states if states is not None else {7: "closed"}, touched or set(), task_states)

    def by_title(self, built: dict) -> dict[str, str]:
        return {i["title"]: i["section"] for i in built["aliased"].values()}

    def test_sections_and_maturity_are_decided_by_code_not_by_the_model(self) -> None:
        sections_ = self.by_title(self.built())
        self.assertEqual(sections_["이메일 로그인 확정"], "decided")
        self.assertEqual(sections_["예전에 반영한 결정"], "unclear")  # 성숙도 누락으로 사람 합의를 추론하지 않는다
        self.assertEqual(sections_["알림 기능"], "in_progress")  # 합의 + GitHub 연결
        self.assertEqual(sections_["로그인 API 만들기"], "in_progress")
        for title in ("소셜 로그인 가안", "소셜 로그인 안", "결제 수단 미정", "인터뷰 질문지"):
            self.assertEqual(sections_[title], "unclear", title)  # 가안·논의·미정·담당 미정은 확정처럼 보이지 않는다
        self.assertNotIn("출처 없는 결정", sections_)
        self.assertEqual(self.built()["excluded"], 1)  # 근거를 못 찾은 항목은 빼고 건수만 밝힌다

    def test_the_answer_cannot_move_items_or_invent_ids(self) -> None:
        built = self.built()
        alias = {i["title"]: a for a, i in built["aliased"].items()}
        answer = {"lines": [{"item": "i999", "note": "없는 항목"},
                            {"item": alias["소셜 로그인 가안"], "note": "초안 단계라 더 논의가 필요해요"},
                            {"item": alias["이메일 로그인 확정"], "note": "기능이 검증 완료됐어요"},
                            {"item": alias["결제 수단 미정"], "note": "결제 수단 미정(논의 중)"}],
                  "overview": {"text": "로그인이 정해졌어요", "evidence": ["i999"]}}
        text = leading.render_briefing(answer, built)
        self.assertNotIn("없는 항목", text)
        self.assertIn("초안 단계라 더 논의가 필요해요", text)
        self.assertNotIn("검증 완료", text)  # 검증 표현이 든 설명은 버리고 줄은 그대로 보인다
        self.assertNotIn("결제 수단 미정(논의 중)", text)  # 제목을 되풀이할 뿐인 설명은 붙이지 않는다
        self.assertIn("이메일 로그인 확정", text)
        self.assertNotIn("로그인이 정해졌어요", text)  # 근거 id가 입력에 없는 개요는 보이지 않는다
        decided = text.split("📌 결정됨")[1].split("🔨 진행 중")[0]
        self.assertIn("[합의] 이메일 로그인 확정", decided)
        self.assertNotIn("가안", decided)  # 가안은 결정됨에 섞이지 않는다
        unclear = text.split("❓ 불명확")[1]
        self.assertIn("[합의 여부 미확인] 예전에 반영한 결정", unclear)
        self.assertNotIn("확정, 성숙도 미기록", text)
        self.assertIn("[가안] 소셜 로그인 가안", unclear)
        self.assertIn("[논의] 결제 수단 미정", unclear)

    def test_every_line_has_a_source_with_a_date_and_alternatives_are_counted(self) -> None:
        built = self.built()
        text = leading.render_briefing({"lines": [], "overview": None}, built, tab_url="http://tab.example/board")
        body = [line for line in text.splitlines() if line.startswith("- ")]
        self.assertEqual(len(body), len(built["aliased"]))
        for line in body:
            self.assertRegex(line, r"근거 \[\d+\]", line)
        self.assertIn("[1] 2026-10-05 회의(2026.10.05) — https://www.notion.so/meet1", text)
        self.assertIn("[합의] 이메일 로그인 확정 · 대안 1건", text)
        self.assertIn("http://tab.example/board", text)
        self.assertIn("근거 출처를 찾지 못해 뺀 항목 1건", text)

    def test_github_status_keeps_the_existing_classification_and_marks_inferred_links(self) -> None:
        closed = leading.render_briefing({"lines": [], "overview": None}, self.built({7: "closed"}))
        self.assertIn("알림 기능 · ✅ 이슈 종료 (추정 연결)", closed)
        self.assertNotIn("검증", closed)  # 이슈 종료를 검증 완료로 말하지 않는다
        opened = leading.render_briefing({"lines": [], "overview": None}, self.built({7: "open"}, {7}))
        self.assertIn("알림 기능 · 🔨 진행 중(커밋 있음) (추정 연결)", opened)
        only_issue = leading.render_briefing({"lines": [], "overview": None}, self.built({7: "open"}))
        self.assertIn("알림 기능 · 📋 이슈만 등록 (추정 연결)", only_issue)
        explicit = self.make("requirement", "명시 연결 요구", self.doc, maturity="agreed", refs=[{"type": "issue", "number": 8, "basis": "explicit"}])
        text = leading.render_briefing({"lines": [], "overview": None}, self.built({7: "open", 8: "closed"}))
        line = next(ln for ln in text.splitlines() if "명시 연결 요구" in ln)
        self.assertIn("✅ 이슈 종료", line)
        self.assertNotIn("추정", line)
        del explicit

    def test_github_state_matches_main_classifier_for_the_same_inputs(self) -> None:
        cases = [({1: "closed"}, set()), ({1: "open"}, {1}), ({1: "open"}, set()), ({}, set())]
        for states, touched in cases:
            expected = main._classify_tasks_by_github_state(["김팀원: 일 #1"], states, touched)
            found = leading.github_state([{"type": "issue", "number": 1, "basis": "explicit"}], states, touched)
            actual = {"done": [], "in_progress": [], "not_started": [], "unlinked": []}
            actual[found["key"] if found else "unlinked"].append("김팀원: 일 #1")
            self.assertEqual(actual, expected, (states, touched))

    def test_task_states_come_from_the_existing_classification(self) -> None:
        built = self.built(task_states={self.task: "in_progress"})
        task = next(i for i in built["aliased"].values() if i["title"] == "로그인 API 만들기")
        self.assertEqual(task["github"]["label"], "🔨 진행 중(커밋 있음)")
        default = next(i for i in self.built()["aliased"].values() if i["title"] == "로그인 API 만들기")
        self.assertEqual(default["github"]["label"], "🆕 이슈 미등록")

    def test_payload_fits_the_budget_and_uses_short_ids(self) -> None:
        for n in range(40):
            self.make("open", f"미정 {n} " + "긴 제목 " * 10, self.meet, maturity="discussed", statement="설명 " * 100)
        built = self.built()
        docs = [{"title": "문서", "type": "기획", "date": "2026-10-02", "summary": "요약 " * 200}]
        payload = leading.fit_payload(built, docs)
        self.assertLessEqual(core.estimate_tokens(json.dumps(payload, ensure_ascii=False)), leading.BRIEFING_BUDGET_TOKENS)
        self.assertTrue(all(i["id"].startswith("i") for i in payload["items"]))
        self.assertTrue(all(len(v) <= leading.MAX_PER_SECTION for v in payload["sections"].values()))


class MenuIntegrationTests(Base):
    def setUp(self) -> None:
        super().setUp()
        self.meet = self.add_page("meet-1", "루트 > 회의록 > 2026-10-05 회의", "meeting", "2026-10-05")
        self.doc = self.add_page("doc-1", "루트 > 기획 > 최종 기획서", "document", "2026-10-02")
        self.make("artifact", "최종 기획서", self.doc, statement="- 본문: 로그인 기획\n\n원문: https://www.notion.so/doc1", extra={"artifact_type": "기획"})
        self.decision = self.make("decision", "이메일 로그인 확정", self.meet, maturity="agreed")
        self.make("open", "결제 수단 미정", self.meet, maturity="discussed")
        self.calls: list[tuple[str, dict]] = []
        self.patches = [patch.object(worklog_bridge, "WORKLOG_STORE", self.store),
                        patch.object(main, "_github_get_cached", new=AsyncMock(return_value=[])),
                        patch.object(main, "_get_commit_touched_issues", new=AsyncMock(return_value=set())),
                        patch.object(main, "_stored_notes", new=AsyncMock(return_value={"texts": ["회의 원문"], "as_of": None})),
                        patch.object(worklog_bridge, "lead_call", self.fake_lead)]
        for p in self.patches:
            p.start()
        self.config = {"notion_page_id": "root", "notion_token": "t", "github_token": "g", "github_repo": "o/r"}

    def tearDown(self) -> None:
        for p in self.patches:
            p.stop()
        super().tearDown()

    def fake_lead(self, name: str, payload: dict):
        self.calls.append((name, payload))
        if name == "project-info":
            answer = {"paragraphs": [{"text": "로그인 기획이 나왔어요.", "sources": ["s1"]}]}
        else:
            first = payload["items"][0]["id"]
            answer = {"lines": [{"item": first, "note": "가장 먼저 볼 항목"}], "overview": None}
        return answer, {"prompt_tokens": 321, "completion_tokens": 45}, "fake:model"

    def status(self) -> str:
        text, _ = asyncio.run(main.fetch_team_status(self.config, SCOPE))
        return text

    def test_menu2_uses_the_briefing_and_a_second_open_does_not_call_the_model(self) -> None:
        text = self.status()
        self.assertIn("📌 결정됨", text)
        self.assertIn("[합의] 이메일 로그인 확정", text)
        self.assertIn("📎 근거", text)
        self.assertEqual([c[0] for c in self.calls], ["project-leading"])
        again = self.status()
        self.assertEqual(again, text)
        self.assertEqual(len(self.calls), 1)  # 같은 입력이면 모델을 다시 부르지 않는다
        self.assertEqual(main._lead_usage[(SCOPE, "menu2")], {"input": 321, "output": 45})  # 토큰 기록
        self.make("open", "새 쟁점", self.meet, maturity="draft")
        self.status()
        self.assertEqual(len(self.calls), 2)  # 입력이 바뀌면 다시 부른다

    def test_menu2_falls_back_to_the_old_list_when_the_model_fails(self) -> None:
        def broken(name: str, payload: dict):
            raise RuntimeError("모델 오류")

        with patch.object(worklog_bridge, "lead_call", broken):
            text = self.status()
        self.assertIn("팀 진행상황 (회의록 기준)", text)  # 예전 목록형
        self.assertIn("📌 확정된 흐름", text)
        self.assertNotIn("📎 근거", text)

        def bad_contract(name: str, payload: dict):
            return {"lines": "목록 아님"}, {}, "fake"

        with patch.object(worklog_bridge, "lead_call", bad_contract):
            self.assertIn("팀 진행상황 (회의록 기준)", self.status())  # 계약 위반도 대체 표시, 저장하지 않는다
        self.assertEqual(self.status().count("📎 근거") > 0, True)  # 이후 정상 호출은 새 방식

    def project(self) -> str:
        snapshot = {"text": "팀 소개 원문", "as_of": None}
        chunks: list[str] = []
        with patch.object(worklog_bridge, "team_snapshot", lambda *a, **k: snapshot):
            text = asyncio.run(main.fetch_project_info(self.config, SCOPE, on_chunk=chunks.append))
        return text

    def test_menu1_uses_documents_with_sources_and_caches_by_input(self) -> None:
        text = self.project()
        self.assertIn("로그인 기획이 나왔어요. [1]", text)
        self.assertIn("[1] 최종 기획서(2026.10.02) — https://www.notion.so/doc1", text)
        self.assertEqual([c[0] for c in self.calls], ["project-info"])
        self.assertEqual(self.calls[0][1]["docs"][0]["title"], "최종 기획서")
        self.assertEqual(self.project(), text)
        self.assertEqual(len(self.calls), 1)  # 같은 입력으로 두 번 열면 두 번째는 모델을 부르지 않는다
        self.assertEqual(main._lead_usage[(SCOPE, "menu1")], {"input": 321, "output": 45})

    def test_menu1_falls_back_to_the_team_page_summary_without_documents_or_on_failure(self) -> None:
        def broken(name: str, payload: dict):
            raise RuntimeError("모델 오류")

        with patch.object(worklog_bridge, "lead_call", broken), patch.object(main, "_summarize_project_info", new=AsyncMock(return_value="예전 요약")):
            self.assertEqual(self.project().splitlines()[0], "예전 요약")
        main._project_summary_cache.clear()  # 예전 요약 캐시(같은 팀 정보 원문)를 비운다
        with db.write(self.store) as c:
            c.execute("UPDATE work_items SET status = 'cancelled' WHERE kind = 'artifact'")
        with patch.object(main, "_summarize_project_info", new=AsyncMock(return_value="문서 없음 요약")):
            self.assertTrue(self.project().startswith("문서 없음 요약"))
        self.assertEqual(self.calls, [])


    def test_menu2_turn_offers_the_board_button_only_with_the_briefing(self) -> None:
        from fastapi.testclient import TestClient

        root = Path(self.tmp.name)
        config_path = root / "team_configs.json"
        config_path.write_text(json.dumps({SCOPE: dict(self.config, admin_aad_id="admin")}), encoding="utf-8")
        body = {"user": {"aad_id": "admin", "name": "관리자"}, "team": {"id": "T1", "name": "알파"}, "text": "",
                "value": {"action": "menu", "choice": "2"}, "conversation": {"id": "C1", "type": "channel"},
                "message_id": "m", "sent_at": "2026-11-04T01:00:00Z"}
        with patch.object(main, "TEAM_CONFIG_PATH", str(config_path)), patch.object(main, "MEMBER_PROFILE_PATH", str(root / "m.json")):
            reply = TestClient(main.app).post("/api/turn", json=body, headers={"X-Bot-Secret": "test-secret"}).text
        events = [json.loads(line) for line in reply.splitlines() if line.strip()]
        urls = [b.get("url") for e in events if e["type"] == "buttons" for b in e["buttons"]]
        self.assertTrue(any(u and "/tab/board?t=" in u for u in urls), events)
        self.assertTrue(any("📎 근거" in (e.get("text") or "") for e in events))


if __name__ == "__main__":
    unittest.main()
