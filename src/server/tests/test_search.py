"""지식 검색 색인·질의, 지식 모델 확장, 마이그레이션 9 테스트. 문서: docs/implementation/v2.1/plan.md 2.3, Phase 1.

데이터는 모두 지어낸 것이다(실제 회의 내용을 넣지 않는다).
"""

from __future__ import annotations

import isolation  # noqa: F401,I001  실제 저장소·키를 쓰지 않게(맨 먼저)

import json
import sqlite3
import statistics
import sys
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from worklog import apply, core, db, search  # noqa: E402

SCOPE = "team:alpha"
OTHER = "team:beta"


def add(store: Path, kind: str, title: str, statement: str = "", scope: str = SCOPE, **extra) -> str:
    """항목 하나를 지식에 넣는다(색인은 쓰는 경로가 같은 트랜잭션에서 갱신한다)."""
    db.initialize(store)
    with db.write(store) as c:
        return core._create_item(c, scope, kind, {"title": title, "statement": statement or title, **extra}, "테스트", "tester", db.now())


def set_status(store: Path, item_id: str, status: str) -> None:
    with db.write(store) as c:
        core._write_item(c, item_id, {"status": status}, "테스트", "tester", db.now())


class SearchTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Path(self.tmp.name) / "w.sqlite3"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def ids(self, text: str, **kwargs) -> list[str]:
        return [r["id"] for r in search.query(self.store, SCOPE, text, **kwargs)["records"]]

    def seed(self) -> dict[str, str]:
        ids = {
            "mirror": add(self.store, "decision", "미러 디스플레이를 쓰기로 했다", "카메라 대신 거울 화면으로 옷차림을 보여 준다"),
            "umbrella": add(self.store, "decision", "공유 우산 대여 서비스로 간다", "비 오는 날 우산을 빌려 준다"),
            "target": add(self.store, "open", "타깃 고객은 누구인가", "20대 직장인인지 대학생인지 아직 모른다"),
            "ui": add(self.store, "task", "UI 시안 만들기", "화면 시안을 이번 주까지 만든다", assignee="김디자", due="2026-11-10"),
            "fb_clothes": add(self.store, "feedback", "옷 정리 앱은 이미 많다", "비슷한 서비스와 차이를 설명해야 한다"),
            "in_clothes": add(self.store, "insight", "옷장 정리에 시간이 오래 걸린다", "주말마다 한 시간 넘게 쓴다는 응답"),
        }
        return ids


class ShortQueryTests(SearchTestCase):
    def test_one_and_two_character_queries_reach_the_items(self) -> None:  # A: 단문 검색
        ids = self.seed()
        found = self.ids("옷")  # 1글자
        self.assertEqual(set(found), {ids["fb_clothes"], ids["in_clothes"], ids["mirror"]})  # 제목·본문에 "옷"이 있는 것
        self.assertEqual(self.ids("미러"), [ids["mirror"]])  # 2글자
        self.assertEqual(self.ids("타깃"), [ids["target"]])
        self.assertEqual(self.ids("UI"), [ids["ui"]])  # 영문 2글자
        self.assertEqual(self.ids("ui"), [ids["ui"]])  # 대소문자 무관

    def test_particles_are_stripped_from_short_words(self) -> None:
        ids = self.seed()
        self.assertEqual(self.ids("미러를"), [ids["mirror"]])
        self.assertEqual(self.ids("타깃은"), [ids["target"]])
        self.assertEqual(search.search_terms("미러를 보여 주는 이유는"), ["미러", "보여", "주는", "이유"])

    def test_one_character_words_inside_a_question_are_ignored_but_a_lone_one_is_kept(self) -> None:
        self.assertEqual(search.search_terms("옷"), ["옷"])
        self.assertEqual(search.search_terms("이 그 어디에"), ["어디"])  # 질문 속 한 글자("이", "그")는 버린다

    def test_short_path_is_used_only_for_short_terms(self) -> None:
        self.seed()
        short = search.query(self.store, SCOPE, "미러")["searched"]["paths"]
        self.assertEqual((short["match"], short["like"]), (0, 1))
        long = search.query(self.store, SCOPE, "서비스")["searched"]["paths"]  # 3글자 이상은 MATCH만
        self.assertGreater(long["match"], 0)
        self.assertEqual(long["like"], 0)
        mixed = search.query(self.store, SCOPE, "우산 서비스")["searched"]["paths"]  # 섞이면 두 경로를 다 쓴다
        self.assertGreater(mixed["match"], 0)
        self.assertGreater(mixed["like"], 0)

    def test_special_characters_in_a_query_do_not_break_the_search(self) -> None:
        self.seed()
        for text in ('%', '_', '"미러"', "미러' OR 1=1 --", "\\", "(", ")", "*"):
            search.query(self.store, SCOPE, text)  # 오류 없이 끝나면 된다


class GeneralQueryTests(SearchTestCase):
    def test_multi_word_queries_use_match_and_rank_the_best_item_first(self) -> None:
        ids = self.seed()
        result = search.query(self.store, SCOPE, "미러 디스플레이 결정")
        self.assertEqual(result["records"][0]["id"], ids["mirror"])
        self.assertGreater(result["searched"]["paths"]["match"], 0)
        self.assertEqual(self.ids("공유 우산 대여")[0], ids["umbrella"])

    def test_adding_the_short_path_does_not_change_a_long_only_ranking(self) -> None:  # 회귀
        ids = self.seed()
        before = self.ids("서비스 우산 대여 비 오는")
        after = self.ids("서비스 우산 대여 비 오는 UI")  # 짧은 검색어를 더해도 앞선 결과의 맨 위는 같다
        self.assertEqual(before[0], ids["umbrella"])
        self.assertEqual(after[0], before[0])

    def test_both_paths_merge_without_duplicates(self) -> None:
        ids = self.seed()
        found = self.ids("UI 시안 만들기")  # "UI"·"시안"은 부분 일치, "만들기"는 MATCH — 같은 항목이 한 번만
        self.assertEqual(found.count(ids["ui"]), 1)
        self.assertEqual(found[0], ids["ui"])

    def test_scope_is_isolated(self) -> None:
        self.seed()
        other = add(self.store, "decision", "다른 팀의 미러 결정", scope=OTHER)
        self.assertNotIn(other, self.ids("미러"))
        self.assertEqual([r["id"] for r in search.query(self.store, OTHER, "미러")["records"]], [other])

    def test_kind_filter_and_empty_query(self) -> None:
        ids = self.seed()
        self.assertEqual(self.ids("옷", kinds=["feedback"]), [ids["fb_clothes"]])
        empty = search.query(self.store, SCOPE, "   ")
        self.assertEqual((empty["records"], empty["markdown"]), ([], ""))
        self.assertEqual(search.query(self.store, SCOPE, "없는단어없음")["records"], [])

    def test_markdown_carries_title_kind_and_id(self) -> None:
        ids = self.seed()
        markdown = search.query(self.store, SCOPE, "미러")["markdown"]
        self.assertIn("미러 디스플레이를 쓰기로 했다", markdown)
        self.assertIn("결정", markdown)
        self.assertIn(ids["mirror"], markdown)


class StatusRankingTests(SearchTestCase):
    def test_superseded_and_resolved_rank_below_live_items_and_deleted_are_excluded(self) -> None:
        old = add(self.store, "decision", "카메라를 항상 켜 둔다", "항상 촬영")
        new = add(self.store, "decision", "카메라는 필요할 때만 켠다", "필요할 때 촬영")
        with db.write(self.store) as c:
            core._write_item(c, old, {"status": "superseded"}, "대체", "tester", db.now())
            core._link(c, new, old, core.LINK_SUPERSEDES, "cand-1", db.now())
        asked = add(self.store, "open", "카메라 각도는 어떻게 할까")
        set_status(self.store, asked, "resolved")
        gone = add(self.store, "decision", "카메라 대신 센서만 쓴다")
        set_status(self.store, gone, "cancelled")
        reverted = add(self.store, "decision", "카메라 두 대를 쓴다")
        set_status(self.store, reverted, "reverted")

        result = search.query(self.store, SCOPE, "카메라")
        found = [r["id"] for r in result["records"]]
        self.assertEqual(found[0], new)  # 살아 있는 것이 먼저
        self.assertLess(found.index(new), found.index(old))
        self.assertLess(found.index(new), found.index(asked))
        self.assertNotIn(gone, found)
        self.assertNotIn(reverted, found)
        self.assertEqual(result["searched"]["deleted_excluded"], 2)
        self.assertEqual(result["conflicts"], [{"new": {"id": new, "title": "카메라는 필요할 때만 켠다"},
                                                 "old": {"id": old, "title": "카메라를 항상 켜 둔다"}}])
        self.assertIn("대체", result["markdown"])
        with_deleted = [r["id"] for r in search.query(self.store, SCOPE, "카메라", include_deleted=True)["records"]]
        self.assertIn(gone, with_deleted)
        self.assertLess(with_deleted.index(new), with_deleted.index(old))

    def test_links_bring_related_items_into_the_result(self) -> None:
        decision = add(self.store, "decision", "주간 스프린트로 진행한다")
        task = add(self.store, "task", "스프린트 보드 만들기")
        with db.write(self.store) as c:
            core._link(c, decision, task, core.LINK_AFFECTS, "cand-2", db.now())
        result = search.query(self.store, SCOPE, "주간 스프린트로 진행한다", limit=1)
        self.assertEqual([r["id"] for r in result["records"]], [decision])
        self.assertEqual([x["id"] for x in result["related"]], [task])


class BudgetTests(SearchTestCase):
    def test_result_never_exceeds_the_budget_and_reports_omissions(self) -> None:
        for n in range(30):
            add(self.store, "feedback", f"예산검색 항목 {n}", "예산검색 " + "긴 설명이 계속 이어진다. " * 20)
        for limit in (200, 400, 1000):
            result = search.query(self.store, SCOPE, "예산검색", limit=30, max_tokens=limit)
            self.assertLessEqual(core.estimate_tokens(result["markdown"]), limit)
            self.assertLessEqual(result["searched"]["used_tokens"], limit)
        small = search.query(self.store, SCOPE, "예산검색", limit=30, max_tokens=400)
        self.assertGreater(small["searched"]["omitted"], 0)  # 빠진 기록 수가 조회 범위에 남는다
        self.assertEqual(small["searched"]["returned"] + small["searched"]["omitted"], 30)
        self.assertEqual(len(small["records"]), small["searched"]["returned"])

    def test_a_first_record_bigger_than_the_budget_is_cut_not_refused(self) -> None:
        add(self.store, "artifact", "아주 긴 문서", "긴문서검색 " + "문장이 끝없이 이어진다. " * 2000)
        result = search.query(self.store, SCOPE, "긴문서검색", max_tokens=200)
        self.assertEqual(len(result["records"]), 1)
        self.assertTrue(result["searched"]["truncated"])
        self.assertIn("이하 생략", result["markdown"])
        self.assertLessEqual(core.estimate_tokens(result["markdown"]), 200)

    def test_a_tiny_budget_still_stays_inside_it(self) -> None:
        add(self.store, "decision", "작은예산 검색 대상", "본문")
        result = search.query(self.store, SCOPE, "작은예산", max_tokens=20)
        self.assertLessEqual(core.estimate_tokens(result["markdown"]), 20)


class IndexMaintenanceTests(SearchTestCase):
    def test_index_follows_creates_and_updates(self) -> None:
        item = add(self.store, "decision", "초기값을 정하는 결정")
        self.assertEqual(self.ids("초기값을 정하는 결정"), [item])
        with db.write(self.store) as c:
            c.execute("UPDATE work_items SET title = ?, normalized_title = ? WHERE id = ?", ("바뀐 이름의 결정", db.normalize_text("바뀐 이름의 결정"), item))
            core._write_item(c, item, {"statement_md": "본문도 바뀜"}, "테스트", "tester", db.now())
        self.assertEqual(self.ids("바뀐 이름의 결정"), [item])
        self.assertEqual(self.ids("초기값"), [])  # 옛 제목에만 있던 단어는 더 이상 걸리지 않는다
        set_status(self.store, item, "cancelled")  # 논리 삭제는 검색에서 빠진다
        self.assertEqual(self.ids("바뀐 이름의 결정"), [])

    def test_attribute_values_are_searchable(self) -> None:
        item = add(self.store, "task", "배포 준비", assignee="박개발")
        add(self.store, "decision", "표현 방식 정하기", maturity="agreed")
        self.assertEqual(self.ids("박개발"), [item])
        self.assertEqual(len(self.ids("agreed")), 1)

    def test_rebuild_after_dropping_the_index_gives_the_same_results(self) -> None:
        self.seed()
        queries = ["옷", "미러", "UI", "타깃", "미러를", "미러 디스플레이 결정", "공유 우산 대여", "옷장 정리"]
        before = {q: [(r["id"], r["score"]) for r in search.query(self.store, SCOPE, q)["records"]] for q in queries}
        other = add(self.store, "decision", "다른 팀 항목", scope=OTHER)
        with closing(sqlite3.connect(self.store)) as c:
            c.execute("DELETE FROM knowledge_fts")
            c.commit()
        self.assertEqual(search.query(self.store, SCOPE, "미러")["records"], [])  # 색인이 비었다
        self.assertEqual(search.rebuild(self.store), 7)  # 전체 다시
        after = {q: [(r["id"], r["score"]) for r in search.query(self.store, SCOPE, q)["records"]] for q in queries}
        self.assertEqual(before, after)
        self.assertEqual(search.rebuild(self.store, SCOPE), 6)  # 한 팀만 다시: 다른 팀 색인은 그대로
        self.assertEqual([r["id"] for r in search.query(self.store, OTHER, "다른 팀")["records"]], [other])
        self.assertEqual(search.rebuild(self.store, SCOPE), 6)  # 여러 번 해도 같다
        self.assertEqual(search.query(self.store, SCOPE, "미러")["searched"]["indexed"], 6)


class RelevanceAndPerformanceTests(SearchTestCase):
    TOPICS = [
        ("decision", "공유 우산 대여로 방향을 정했다", "우산"), ("decision", "대여 보증금은 받지 않는다", "보증금"),
        ("open", "우산 분실은 누가 책임지나", "분실"), ("task", "정류장 위치 조사하기", "정류장"),
        ("insight", "비 오는 날 출근길에 우산이 없다는 응답", "출근길"), ("feedback", "보관함 크기를 먼저 정하라는 조언", "보관함"),
        ("decision", "결제는 간편결제만 연결한다", "간편결제"), ("task", "UI 와이어프레임 그리기", "와이어프레임"),
        ("open", "앱 이름은 무엇으로 할까", "앱 이름"), ("insight", "대학생은 하루 단위 대여를 선호한다", "하루 단위"),
        ("decision", "시범 운영은 캠퍼스 두 곳에서 한다", "캠퍼스"), ("task", "운영 매뉴얼 초안 쓰기", "매뉴얼"),
        ("feedback", "손잡이 색을 눈에 띄게 하라는 의견", "손잡이"), ("open", "비가 그친 뒤 반납은 어떻게 하나", "반납"),
    ]
    SHORT = [("옷", "옷걸이 수거함 위치 정하기"), ("비", "비 예보 연동 여부 논의"), ("UI", "UI 색상 규칙"), ("앱", "앱 설치 없이 쓰는 방법"),
             ("QR", "QR 코드로 대여 시작"), ("몰", "쇼핑몰 입점 가능성")]

    def test_relevance_over_a_query_set_of_twenty(self) -> None:
        expected: dict[str, str] = {}
        for kind, title, key in self.TOPICS:
            expected[key] = add(self.store, kind, title, title + " 관련 논의 메모")
        shorts = {}
        for key, title in self.SHORT:
            shorts[key] = add(self.store, "task", title, title + " 세부 내용")
        queries = [(key, item) for key, item in expected.items()] + [(key, item) for key, item in shorts.items()]
        self.assertEqual(len(queries), 20)
        hits = 0
        misses = []
        for key, item in queries:
            top = self.ids(key, limit=5)
            if item in top:
                hits += 1
            else:
                misses.append(key)
        ratio = hits / len(queries)
        print(f"\n[관련성] 질의 20개(단문 6) 중 상위 5개 안에 기대 항목: {hits}/20 = {ratio:.0%}  빗나감: {misses}")
        self.assertGreaterEqual(ratio, 0.8)

    def test_short_query_speed_on_five_thousand_items(self) -> None:
        db.initialize(self.store)
        words = ["우산", "대여", "정류장", "결제", "보증금", "캠퍼스", "분실", "반납", "매뉴얼", "와이어프레임", "옷", "UI", "앱"]
        with db.write(self.store) as c:
            for n in range(5000):
                title = f"{words[n % len(words)]} {words[(n * 7) % len(words)]} 항목 {n}"
                core._create_item(c, SCOPE, "feedback", {"title": title, "statement": title + " 설명 " + words[(n * 3) % len(words)]},
                                  "합성", "tester", db.now())
        self.assertEqual(search.query(self.store, SCOPE, "항목 4999")["searched"]["indexed"], 5000)
        timings = []
        for term in ("옷", "UI", "앱", "옷 UI", "결제 앱"):
            for _ in range(4):
                start = time.perf_counter()
                result = search.query(self.store, SCOPE, term)
                timings.append((time.perf_counter() - start) * 1000)
                self.assertTrue(result["records"])
        median, worst = statistics.median(timings), max(timings)
        print(f"\n[성능] 5,000건 단문 질의 {len(timings)}회: 중앙값 {median:.1f}ms, 최대 {worst:.1f}ms")
        self.assertLess(median, 100)


class KnowledgeModelTests(SearchTestCase):
    def attributes(self, item_id: str) -> dict:
        with closing(sqlite3.connect(self.store)) as c:
            return json.loads(c.execute("SELECT attributes_json FROM work_items WHERE id = ?", (item_id,)).fetchone()[0])

    def test_new_kinds_and_attributes_are_stored(self) -> None:
        refs = [{"type": "issue", "number": 12, "basis": "explicit"}, {"type": "pr", "number": 30, "basis": "inferred"}]
        item = add(self.store, "artifact", "결제 연동 기획서", "요약", maturity="agreed", artifact_type="기획", summary="결제 흐름 요약",
                   github_refs=refs)
        attrs = self.attributes(item)
        self.assertEqual((attrs["maturity"], attrs["artifact_type"], attrs["summary"]), ("agreed", "기획", "결제 흐름 요약"))
        self.assertEqual(attrs["github_refs"], refs)
        for kind in ("requirement", "hypothesis"):
            self.assertTrue(add(self.store, kind, f"{kind} 하나"))
        self.assertEqual(self.ids("결제 흐름 요약"), [item])  # summary 속성도 색인된다

    def test_invalid_values_are_rejected(self) -> None:
        with self.assertRaises(db.ValidationError):
            add(self.store, "decision", "성숙도 오류", maturity="final")
        with self.assertRaises(db.ValidationError):
            add(self.store, "memo", "없는 종류")
        with self.assertRaises(db.ValidationError):
            add(self.store, "task", "연결 오류", github_refs=[{"type": "issue", "number": 1, "basis": "guess"}])
        with self.assertRaises(db.ValidationError):
            add(self.store, "task", "연결 오류2", github_refs="#1")

    def test_old_issue_refs_are_read_as_explicit_issues(self) -> None:
        self.assertEqual(core.github_refs({"issue_refs": [7, 9]}),
                         [{"type": "issue", "number": 7, "basis": "explicit"}, {"type": "issue", "number": 9, "basis": "explicit"}])
        mixed = {"github_refs": [{"type": "issue", "number": 3, "basis": "explicit"}, {"type": "issue", "number": 4, "basis": "inferred"},
                                 {"type": "pr", "number": 5, "basis": "explicit"}], "issue_refs": [99]}
        self.assertEqual(core.issue_numbers(mixed), [3])  # 명시적 이슈만(PR·추정 제외, issue_refs는 github_refs가 있으면 무시)
        self.assertEqual(core.issue_numbers(mixed, explicit_only=False), [3, 4])
        self.assertEqual(core.issue_numbers({}), [])

    def test_take_task_hands_back_only_explicit_issue_numbers(self) -> None:
        old_style = add(self.store, "task", "예전 방식 할 일", issue_refs=[21])
        new_style = add(self.store, "task", "새 방식 할 일", github_refs=[{"type": "issue", "number": 31, "basis": "explicit"},
                                                                      {"type": "issue", "number": 32, "basis": "inferred"}])
        self.assertEqual(apply.take_task(self.store, SCOPE, old_style, "김팀원", "김팀원")["issue_refs"], [21])
        self.assertEqual(apply.take_task(self.store, SCOPE, new_style, "박팀원", "박팀원")["issue_refs"], [31])

    def test_resolved_items_do_not_block_a_new_item_with_the_same_title(self) -> None:
        first = add(self.store, "open", "타깃 고객은 누구인가")
        with self.assertRaises(db.ConflictError):  # 살아 있는 같은 제목은 막는다
            add(self.store, "open", "타깃 고객은 누구인가")
        set_status(self.store, first, "resolved")
        self.assertTrue(add(self.store, "open", "타깃 고객은 누구인가"))  # 해결된 뒤에는 새로 만들 수 있다
        live = [i["id"] for i in apply.live_items(self.store, SCOPE).get("open", [])]
        self.assertNotIn(first, live)  # live_items는 resolved를 뺀다
        with db.write(self.store) as c:
            related = core.find_related(c, SCOPE, {"title": "타깃 고객은 누구인가", "statement": "", "issue_refs": []})
        self.assertNotIn(first, [r["id"] for r in related])

    def test_new_link_types_are_known(self) -> None:
        self.assertEqual(core.LINK_TYPES, {"AFFECTS", "SUPERSEDES", "DERIVED_FROM", "RESOLVES", "ALTERNATIVE_TO", "RELATED_TO"})
        a, b = add(self.store, "artifact", "문서"), add(self.store, "decision", "문서에서 나온 결정")
        with db.write(self.store) as c:
            core._link(c, b, a, core.LINK_DERIVED_FROM, "cand-3", db.now())
            self.assertEqual(c.execute("SELECT type FROM work_links").fetchone()[0], "DERIVED_FROM")


class MigrationTests(SearchTestCase):
    def build_v8(self) -> None:
        """schema 8까지만 적용한 저장소에 예전 방식 항목을 직접 넣는다(실제 저장소의 업그레이드를 흉내 낸다)."""
        with closing(sqlite3.connect(self.store)) as c:
            c.execute("CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            for version in range(1, 9):
                for statement in db.MIGRATIONS[version - 1]:
                    c.execute(statement)
            c.execute("INSERT INTO schema_meta(key, value) VALUES ('schema_version', '8')")
            for n, (kind, title) in enumerate([("decision", "미러 화면을 쓴다"), ("open", "타깃은 누구인가"), ("task", "UI 시안 그리기")]):
                attributes = json.dumps({"assignee": "김디자"} if n == 2 else {})
                c.execute("""INSERT INTO work_items(id, scope, kind, title, normalized_title, statement_md, status, attributes_json,
                             version, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, 'active', ?, 1, ?, ?)""",
                          (f"old-{n}", SCOPE, kind, title, db.normalize_text(title), title + " 설명", attributes,
                           "2026-11-01T00:00:00Z", "2026-11-01T00:00:00Z"))
            c.commit()

    def test_upgrade_from_v8_keeps_data_and_fills_the_index(self) -> None:
        self.build_v8()
        with closing(sqlite3.connect(self.store)) as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM sqlite_master WHERE name = 'knowledge_fts'").fetchone()[0], 0)
        result = db.initialize(self.store)
        self.assertEqual(result["schema_version"], db.SCHEMA_VERSION)  # v8 → 최신(9 이후 마이그레이션 포함)
        with closing(sqlite3.connect(self.store)) as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM work_items").fetchone()[0], 3)  # 데이터 보존
            self.assertEqual(c.execute("SELECT COUNT(*) FROM knowledge_fts").fetchone()[0], 3)  # 기존 항목으로 채움
            self.assertEqual(c.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()[0], str(db.SCHEMA_VERSION))
        self.assertEqual(self.ids("미러"), ["old-0"])
        self.assertEqual(self.ids("김디자"), ["old-2"])  # 속성도 색인
        db.initialize(self.store)  # 한 번 더 해도 그대로
        self.assertEqual(search.query(self.store, SCOPE, "미러")["searched"]["indexed"], 3)

    def test_unique_index_now_lets_a_resolved_title_be_reused(self) -> None:
        self.build_v8()
        db.initialize(self.store)
        with self.assertRaises(db.ConflictError):
            add(self.store, "open", "타깃은 누구인가")
        set_status(self.store, "old-1", "resolved")
        self.assertTrue(add(self.store, "open", "타깃은 누구인가"))
        with closing(sqlite3.connect(self.store)) as c:
            sql = c.execute("SELECT sql FROM sqlite_master WHERE name = 'work_items_live_title'").fetchone()[0]
        self.assertIn("'resolved'", sql)

    def test_newer_store_is_still_refused(self) -> None:
        db.initialize(self.store)
        with closing(sqlite3.connect(self.store)) as c:
            c.execute("UPDATE schema_meta SET value = '99' WHERE key = 'schema_version'")
            c.commit()
        with self.assertRaises(db.ValidationError):
            db.initialize(self.store)


if __name__ == "__main__":
    unittest.main()
