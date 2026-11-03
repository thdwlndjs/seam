"""Notion 구조 지도 테스트. 가짜 Notion 워크스페이스로 여러 페이지 구성을 흉내 낸다.
실행: server 폴더에서 python -m unittest discover -s tests
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import notion_map  # noqa: E402
from worklog import core, sections  # noqa: E402

SCOPE = "team:alpha"


def rt(text: str) -> dict:
    return {"rich_text": [{"plain_text": text}]}


def heading(bid: str, text: str, level: int = 1, children: list | None = None) -> dict:
    kind = f"heading_{level}"
    return {"id": bid, "type": kind, kind: dict(rt(text), is_toggleable=bool(children)), "has_children": bool(children),
            "_children": children or []}


def para(bid: str, text: str) -> dict:
    return {"id": bid, "type": "paragraph", "paragraph": rt(text), "has_children": False}


def child_page(bid: str, title: str) -> dict:
    return {"id": bid, "type": "child_page", "child_page": {"title": title}, "has_children": True}


def child_db(bid: str, title: str) -> dict:
    return {"id": bid, "type": "child_database", "child_database": {"title": title}, "has_children": False}


def columns(bid: str, *cols: list) -> dict:
    return {"id": bid, "type": "column_list", "has_children": True,
            "_children": [{"id": f"{bid}-c{i}", "type": "column", "has_children": True, "_children": c} for i, c in enumerate(cols)]}


class FakeNotion:
    """pages.retrieve / blocks.children.list / databases.retrieve / data_sources.query 만 흉내 낸다."""

    def __init__(self) -> None:
        self.pages: dict[str, dict] = {}       # id -> {"title", "edited", "blocks"}
        self.dbs: dict[str, list[dict]] = {}    # id -> rows
        self.db_titles: dict[str, str] = {}
        self.calls: list[str] = []
        outer = self

        class Pages:
            def retrieve(self, page_id):
                outer.calls.append(f"page:{page_id}")
                if page_id in outer.pages:
                    p = outer.pages[page_id]
                    return {"id": page_id, "last_edited_time": p["edited"],
                            "properties": {"title": {"type": "title", "title": [{"plain_text": p["title"]}]}}}
                raise KeyError(page_id)

        class Children:
            def list(self, block_id, start_cursor=None):
                outer.calls.append(f"blocks:{block_id}")
                return {"results": outer._children(block_id), "has_more": False}

        class Blocks:
            children = Children()

        class Databases:
            def retrieve(self, database_id):
                outer.calls.append(f"db:{database_id}")
                title = outer.db_titles.get(database_id, "")
                return {"data_sources": [{"id": database_id}], "title": [{"plain_text": title}],
                        "last_edited_time": "2026-11-01T00:00:00Z"}

        class DataSources:
            def query(self, source_id, start_cursor=None, filter=None, page_size=None):
                outer.calls.append(f"query:{source_id}")
                rows = outer.dbs[source_id]
                if filter:
                    since = filter["last_edited_time"]["on_or_after"]
                    rows = [r for r in rows if r["last_edited_time"] >= since]
                return {"results": rows[:page_size] if page_size else rows, "has_more": False}

        self.pages_api, self.blocks, self.databases, self.data_sources = Pages(), Blocks(), Databases(), DataSources()

    @property
    def pages_(self):  # pragma: no cover - 이름 충돌 회피용
        return self.pages_api

    def _children(self, block_id: str) -> list[dict]:
        if block_id in self.pages:
            return self.pages[block_id]["blocks"]
        for rows in self.dbs.values():
            for row in rows:
                if row["id"] == block_id:
                    return row.get("_body", [])
        found = self._find(block_id, [b for p in self.pages.values() for b in p["blocks"]])
        return found.get("_children", []) if found else []

    def _find(self, block_id: str, blocks: list[dict]) -> dict | None:
        for b in blocks:
            if b["id"] == block_id:
                return b
            hit = self._find(block_id, b.get("_children", []))
            if hit:
                return hit
        return None


class ClientView:
    """notion_client.Client처럼 client.pages / client.blocks ... 로 접근하게 감싼다."""

    def __init__(self, fake: FakeNotion) -> None:
        self.pages, self.blocks, self.databases, self.data_sources = fake.pages_api, fake.blocks, fake.databases, fake.data_sources


def row(rid: str, title: str, edited: str, body: str = "", **props) -> dict:
    properties = {"이름": {"type": "title", "title": [{"plain_text": title}]}}
    for name, value in props.items():
        properties[name] = {"type": "rich_text", "rich_text": [{"plain_text": value}]}
    return {"id": rid, "last_edited_time": edited, "properties": properties,
            "_body": [para(f"{rid}-b", body)] if body else []}


def team_workspace() -> FakeNotion:
    """루트 '팀 정보' 아래에 서로 다른 담는 방식이 섞인 구성."""
    n = FakeNotion()
    n.pages["root"] = {"title": "알파팀 정보", "edited": "2026-11-01T00:00:00Z", "blocks": [
        para("intro", "우리 팀 소개"),
        heading("h-progress", "팀원별 진행상황", 1),
        heading("h-kim", "김팀원", 2),
        para("p-kim", "로그인 API 구현 중 #4"),
        heading("h-lee", "이팀원", 2),
        columns("cols", [para("p-lee1", "배포 파이프라인 작성")], [para("p-lee2", "금요일 완료 목표")]),
        heading("h-meeting", "회의록", 1),
        child_db("db-meeting", "회의 기록"),
        heading("h-notice", "공지사항", 1, children=[para("p-notice", "보안 교육 필수")]),
        child_page("pg-mentor", "멘토링"),
        child_db("db-wbs", "WBS"),
        heading("h-link", "외부 일정", 1),
        {"id": "linked", "type": "unsupported", "unsupported": {}, "has_children": False},
    ]}
    n.pages["pg-mentor"] = {"title": "멘토링", "edited": "2026-11-01T00:00:00Z", "blocks": [
        heading("m1", "1회차", 2), para("m1p", "인증 구조 조언"),
        heading("m2", "2회차", 2), child_page("m2pg", "2회차 상세"),
    ]}
    n.pages["m2pg"] = {"title": "2회차 상세", "edited": "2026-11-01T00:00:00Z", "blocks": [para("m2pgp", "캐시 전략 리뷰")]}
    n.dbs["db-meeting"] = [row("r1", "11/1 주간회의", "2026-11-01T01:00:00Z", body="인증은 OAuth로 확정", 요약="OAuth 결정")]
    n.dbs["db-wbs"] = [row("w1", "1. 인증", "2026-11-01T01:00:00Z", 기간="11/1~11/7")]
    return n


# 팀 워크스페이스 각 섹션에 대해 "사람이 내용을 보고 내렸을" 판정. LLM 자리에 쓴다.
TRUE_ROLES = {
    "팀원별 진행상황": "progress", "김팀원": "progress", "이팀원": "progress", "회의록": "meeting",
    "공지사항": "ignore", "외부 일정": "reference", "WBS": "reference", "1회차": "reference", "2회차": "reference",
    "질문 목록": "reference", "주간 회의 회고": "meeting", "공지사항 (개정)": "ignore", "주간 보고": "progress",
    "역할 분담 원칙": "ignore", "Sprint 3": "progress",
}


class FakeClassifier:
    def __init__(self, roles: dict[str, str] | None = None) -> None:
        self.roles = roles or TRUE_ROLES
        self.calls: list[list[dict]] = []

    def __call__(self, items: list[dict]) -> dict:
        self.calls.append(items)
        return {i["id"]: {"role": self.roles[i["title"]], "confidence": "high", "reason": "내용 확인"}
                for i in items if i["title"] in self.roles}


class NotionMapTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Path(self.tmp.name) / "w.sqlite3"
        self.fake = team_workspace()
        self.client = ClientView(self.fake)
        self.llm = FakeClassifier()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def sync(self, classify="default") -> dict:
        self.fake.calls.clear()
        return notion_map.sync(self.store, SCOPE, "root", self.client, classify=self.llm if classify == "default" else classify)

    def by_title(self) -> dict[str, dict]:
        return {s["title"]: s for s in sections.sections(self.store, SCOPE, include_inactive=True)}

    def records(self) -> list[tuple[str, str, str | None]]:
        with closing(sqlite3.connect(self.store)) as c:
            return c.execute("""SELECT s.locator, s.content, w.hold FROM sources s JOIN work_records w ON w.source_id = s.id
                                ORDER BY s.rowid""").fetchall()

    def due(self) -> list:
        return core.due_batches(self.store, "2099-01-01T00:00:00Z")

    # ------------------------------------------------------------ 구조

    def test_scan_finds_sections_and_their_containers(self) -> None:
        self.sync()
        s = self.by_title()
        self.assertEqual(s["김팀원"]["path"], "알파팀 정보 > 팀원별 진행상황 > 김팀원")
        self.assertEqual(s["회의록"]["layout"]["databases"], [{"id": "db-meeting", "title": "회의 기록"}])
        self.assertEqual(s["WBS"]["layout"]["databases"], [{"id": "db-wbs", "title": "WBS"}])  # 제목 밖의 전체 DB
        self.assertEqual(s["2회차"]["layout"]["pages"], [{"id": "m2pg", "title": "2회차 상세"}])  # 하위 페이지 안의 섹션
        self.assertEqual(s["외부 일정"]["layout"]["unreadable"], ["linked"])

    # ------------------------------------------------------------ 판정

    def test_llm_sees_container_and_a_content_sample(self) -> None:
        self.sync()
        items = {i["title"]: i for i in self.llm.calls[0]}
        self.assertEqual(items["회의록"]["container"], "제목 아래 글, 데이터베이스 1개")
        self.assertIn("(행) 이름: 11/1 주간회의", items["회의록"]["sample"])
        self.assertIn("로그인 API 구현 중", items["김팀원"]["sample"])
        self.assertIn("배포 파이프라인 작성", items["이팀원"]["sample"])  # 열(column) 안 내용
        self.assertIn("보안 교육 필수", items["공지사항"]["sample"])  # 토글 안쪽
        self.assertTrue(all(len(i["sample"]) <= notion_map.SAMPLE_CHARS for i in items.values()))

    def test_judgments_are_used_without_a_confirmation_step(self) -> None:
        self.sync()
        s = self.by_title()
        self.assertEqual((s["김팀원"]["role"], s["김팀원"]["role_source"], s["김팀원"]["role_note"]),
                         ("progress", "llm", "high · 내용 확인"))
        recs = {loc: hold for loc, _, hold in self.records()}
        self.assertIsNone(recs["notion:h-kim"])  # 바로 분석 대상
        self.assertEqual(recs["notion:w1"], "reference")
        self.assertEqual(recs["notion:h-notice"], "reference")  # ignore도 저장(팀 정보용), 분석은 안 함
        with closing(sqlite3.connect(self.store)) as c:
            kim = c.execute("SELECT id FROM sources WHERE locator = 'notion:h-kim'").fetchone()[0]
        self.assertIn(kim, {sid for b in self.due() for sid in b["source_ids"]})

    def test_misleading_titles_are_judged_by_content(self) -> None:
        root = self.fake.pages["root"]
        root["blocks"] += [heading("h-rule", "역할 분담 원칙", 1), para("p-rule", "PR은 2명 리뷰 후 머지한다"),
                           heading("h-sprint", "Sprint 3", 1), para("p-sprint", "김: 결제 연동, 이: 알림 발송")]
        self.sync()
        s = self.by_title()
        self.assertEqual((s["역할 분담 원칙"]["role"], s["Sprint 3"]["role"]), ("ignore", "progress"))
        self.assertEqual([h for loc, _, h in self.records() if loc == "notion:h-rule"], ["reference"])

    def test_low_confidence_is_kept_but_not_analyzed(self) -> None:
        def unsure(items):
            return {i["id"]: {"role": "progress", "confidence": "low", "reason": "애매함"} for i in items if i["title"] == "김팀원"}
        self.sync(classify=unsure)
        kim = self.by_title()["김팀원"]
        self.assertEqual(kim["role"], "reference")
        self.assertIn("확신 낮음(progress)", kim["role_note"])
        self.assertEqual([h for loc, _, h in self.records() if loc == "notion:h-kim"], ["reference"])

    def test_without_llm_everything_is_kept_and_one_fix_releases_a_whole_branch(self) -> None:
        self.sync(classify=None)
        s = self.by_title()
        self.assertEqual((s["팀원별 진행상황"]["role"], s["팀원별 진행상황"]["role_source"]), ("reference", "none"))
        self.assertEqual(s["김팀원"]["role_source"], "inherit")
        self.assertEqual(self.due(), [])  # 원문은 다 쌓였지만 분석은 안 됨
        self.assertIn("notion:h-notice", {loc for loc, _, _ in self.records()})  # 모르는 섹션도 버리지 않음
        result = sections.set_role(self.store, "h-progress", "progress", "lead")
        self.assertEqual(sorted(result["inherited_by"]), ["h-kim", "h-lee"])
        self.assertGreaterEqual(result["released_records"], 2)
        self.assertEqual(self.by_title()["이팀원"]["role"], "progress")
        self.assertTrue(self.due())

    def test_classifier_failure_keeps_raw_only(self) -> None:
        def broken(items):
            raise RuntimeError("groq down")
        self.sync(classify=broken)
        self.assertEqual(self.by_title()["회의록"]["role"], "reference")
        self.assertEqual(self.due(), [])

    def test_rescan_judges_only_new_or_renamed_sections(self) -> None:
        self.sync()
        sections.set_role(self.store, "h-link", "ignore", "lead")
        root = self.fake.pages["root"]
        root["edited"] = "2026-11-02T00:00:00Z"
        root["blocks"].append(heading("h-weekly", "주간 보고", 1))
        notice = next(b for b in root["blocks"] if b["id"] == "h-notice")
        notice["heading_1"]["rich_text"] = [{"plain_text": "공지사항 (개정)"}]
        link = next(b for b in root["blocks"] if b["id"] == "h-link")
        link["heading_1"]["rich_text"] = [{"plain_text": "외부 일정 (구글)"}]
        stats = self.sync()
        self.assertEqual(sorted(i["title"] for i in self.llm.calls[-1]), ["공지사항 (개정)", "주간 보고"])
        self.assertEqual(stats["judged"], 2)
        s = self.by_title()
        self.assertEqual(s["주간 보고"]["role"], "progress")
        self.assertEqual((s["외부 일정 (구글)"]["role"], s["외부 일정 (구글)"]["role_source"]), ("ignore", "human"))

    # ------------------------------------------------------------ 머리말·루트 변경·저장본

    def test_text_above_the_first_heading_is_an_intro_section(self) -> None:
        self.sync()
        intro = self.by_title()["(머리말)"]
        self.assertEqual(intro["id"], "root:intro")
        self.assertIn("우리 팀 소개", {loc: t for loc, t, _ in self.records()}["notion:root:intro"])

    def test_changing_the_root_page_retires_the_old_map(self) -> None:
        self.sync()
        self.fake.pages["root2"] = {"title": "새 팀 페이지", "edited": "2026-11-05T00:00:00Z",
                                    "blocks": [heading("n1", "주간 보고", 1), para("n1p", "새 기록")]}
        self.fake.calls.clear()
        stats = notion_map.sync(self.store, SCOPE, "root2", self.client, classify=self.llm)
        self.assertTrue(any("김팀원" in p for p in stats["sections_removed"]))
        active = {s["title"] for s in sections.sections(self.store, SCOPE)}
        self.assertEqual(active, {"주간 보고"})

    def test_snapshot_rebuilds_the_page_from_stored_records_in_page_order(self) -> None:
        self.sync()
        snap = sections.snapshot(self.store, SCOPE)
        text = snap["text"]
        self.assertLess(text.index("우리 팀 소개"), text.index("로그인 API 구현 중"))  # 머리말이 먼저
        self.assertIn("보안 교육 필수", text)  # 분석 제외 섹션도 팀 정보엔 들어간다
        self.assertIn("인증은 OAuth로 확정", text)
        self.assertTrue(snap["as_of"])
        # 행이 수정되면 최신 버전만 들어간다
        self.fake.dbs["db-meeting"][0] = row("r1", "11/1 주간회의", "2026-11-01T02:00:00Z", body="인증은 SSO로 변경", 요약="SSO")
        self.sync()
        text = sections.snapshot(self.store, SCOPE)["text"]
        self.assertIn("인증은 SSO로 변경", text)
        self.assertNotIn("인증은 OAuth로 확정", text)

    def test_snapshot_is_none_before_first_scan(self) -> None:
        self.assertIsNone(sections.snapshot(self.store, SCOPE))

    # ------------------------------------------------------------ 루트 여러 개 (팀 정보 + 회의록)

    def test_separate_notes_database_becomes_its_own_root(self) -> None:
        self.fake.dbs["notes-db"] = [row("n1", "11/3 회의", "2026-11-03T01:00:00Z", body="배포는 금요일, 담당 김팀원")]
        self.fake.db_titles["notes-db"] = "회의록 DB"
        stats = notion_map.sync_roots(self.store, SCOPE, ["root", "notes-db"], self.client, classify=FakeClassifier(
            dict(TRUE_ROLES, **{"회의록 DB": "meeting"})))
        self.assertEqual(stats["roots"]["notes-db"], "synced")
        self.assertEqual(self.by_title()["회의록 DB"]["role"], "meeting")
        notes = sections.meeting_notes(self.store, SCOPE, "notes-db")
        self.assertTrue(any("배포는 금요일" in t for t in notes["texts"]))
        self.assertNotIn("배포는 금요일", sections.snapshot(self.store, SCOPE, root_id="root")["text"])  # 1번엔 팀 페이지만

    def test_notes_inside_the_team_page_are_not_read_twice(self) -> None:
        stats = notion_map.sync_roots(self.store, SCOPE, ["root", "db-meeting"], self.client, classify=self.llm)
        self.assertEqual(stats["roots"]["db-meeting"], "inside first root")
        self.assertEqual(sum(1 for loc, _, _ in self.records() if loc == "notion:r1"), 1)
        notes = sections.meeting_notes(self.store, SCOPE, "db-meeting")
        self.assertTrue(any("인증은 OAuth로 확정" in t for t in notes["texts"]))

    def test_dropping_the_notes_root_retires_only_its_map(self) -> None:
        self.fake.dbs["notes-db"] = [row("n1", "11/3 회의", "2026-11-03T01:00:00Z", body="x")]
        self.fake.db_titles["notes-db"] = "회의록 DB"
        notion_map.sync_roots(self.store, SCOPE, ["root", "notes-db"], self.client, classify=self.llm)
        notion_map.sync_roots(self.store, SCOPE, ["root"], self.client, classify=self.llm)
        titles = {s["title"] for s in sections.sections(self.store, SCOPE)}
        self.assertNotIn("회의록 DB", titles)
        self.assertIn("김팀원", titles)

    def shared_heading_workspace(self) -> str:
        """실제 팀 구성: 한 제목 아래 WBS와 멘토링(회의록) DB가 함께 있고, Notion은 하이픈 있는 ID를 돌려준다.
        설정에 저장되는 회의록 ID(하이픈 없음)를 돌려준다."""
        wbs, mentor = "7c911be9-7ce7-83dc-87c1-01ddaf662024", "75e11be9-7ce7-83ea-919f-81730eb17064"
        self.fake.pages["root"]["blocks"] += [heading("h-basic", "프로젝트 기본정보", 1),
                                              child_db(wbs, "프로젝트 WBS"), child_db(mentor, "멘토링")]
        self.fake.dbs[wbs] = [row("wbs-1", "0. 주제선정", "2026-11-01T02:00:00Z", body="주제 아이데이션")]
        self.fake.dbs[mentor] = [row("mt-1", "1차 멘토링", "2026-11-02T02:04:00Z", body="센서 대체제 찾아보기"),
                                 row("mt-2", "2차 멘토링(현직자)", "2026-11-02T02:04:00Z", body="신뢰성이 가장 우선")]
        return mentor.replace("-", "")

    def test_notes_db_sharing_a_heading_returns_only_its_own_rows(self) -> None:
        notes_id = self.shared_heading_workspace()
        stats = notion_map.sync_roots(self.store, SCOPE, ["root", notes_id], self.client, classify=self.llm)
        self.assertEqual(stats["roots"][notes_id], "inside first root")  # 하이픈 유무가 달라도 같은 DB로 본다
        texts = "\n".join(sections.meeting_notes(self.store, SCOPE, notes_id)["texts"])
        self.assertIn("센서 대체제 찾아보기", texts)  # 제목만이 아니라 본문까지
        self.assertIn("신뢰성이 가장 우선", texts)
        self.assertNotIn("주제 아이데이션", texts)  # 같은 제목 아래 WBS 행은 섞이지 않는다
        self.assertEqual(sum(1 for loc, _, _ in self.records() if loc == "notion:mt-1"), 1)  # 두 번 읽지 않는다

    def test_rows_collected_before_parent_tracking_are_backfilled_without_rereading(self) -> None:
        notes_id = self.shared_heading_workspace()
        notion_map.sync_roots(self.store, SCOPE, ["root", notes_id], self.client, classify=self.llm)
        with closing(sqlite3.connect(self.store)) as c, c:
            c.execute("UPDATE source_units SET parent_id = NULL")  # 예전 버전이 모은 저장소
        before = "\n".join(sections.meeting_notes(self.store, SCOPE, notes_id)["texts"])
        self.assertIn("센서 대체제 찾아보기", before)  # 출처를 채우기 전에도 비지 않는다(섹션 전체로 대신)
        self.fake.calls.clear()
        notion_map.sync_roots(self.store, SCOPE, ["root", notes_id], self.client, classify=self.llm)
        self.assertFalse([c for c in self.fake.calls if c in {"blocks:mt-1", "blocks:mt-2", "blocks:wbs-1"}])  # 본문은 다시 안 읽음
        after = "\n".join(sections.meeting_notes(self.store, SCOPE, notes_id)["texts"])
        self.assertIn("센서 대체제 찾아보기", after)
        self.assertNotIn("주제 아이데이션", after)

    def test_notes_root_read_separately_before_is_retired_once_found_inside(self) -> None:
        notes_id = self.shared_heading_workspace()
        # 예전 버전: ID 표기가 달라 회의록 DB를 따로 된 루트로 읽어 둔 지도
        sections.save_page(self.store, SCOPE, notes_id, notes_id, "멘토링", "2026-11-02T02:04:54Z", "fp")
        stats = notion_map.sync_roots(self.store, SCOPE, ["root", notes_id], self.client, classify=self.llm)
        self.assertEqual(stats["roots"][notes_id], "inside first root")
        self.assertNotIn(notes_id, {p["page_id"] for p in sections.pages(self.store, SCOPE)})

    def test_configured_notes_without_any_record_ask_for_a_read(self) -> None:
        self.sync(classify=None)  # 회의 역할로 판정된 섹션이 없는 상태
        self.assertIsNone(sections.meeting_notes(self.store, SCOPE, "not-collected-yet"))  # 호출부가 한 번 읽어 온다
        self.assertIsNotNone(sections.meeting_notes(self.store, SCOPE, None))  # 회의록 설정이 없으면 다시 읽지 않는다

    def test_meeting_notes_keep_the_most_recent_within_the_limit(self) -> None:
        self.fake.dbs["db-meeting"] = [row(f"r{i}", f"{i}차 회의", f"2026-11-{i:02d}T00:00:00Z", body="내용 " * 50) for i in range(1, 10)]
        self.sync()
        notes = sections.meeting_notes(self.store, SCOPE, "db-meeting", max_chars=800)
        self.assertGreater(notes["omitted"], 0)
        self.assertIn("9차 회의", notes["texts"][-1])  # 가장 최근 것이 들어가고, 시간순으로 놓인다

    # ------------------------------------------------------------ 수집

    def test_bodies_are_read_per_container(self) -> None:
        self.sync()
        recs = {loc: (text, hold) for loc, text, hold in self.records()}
        self.assertIn("금요일 완료 목표", recs["notion:h-lee"][0])
        self.assertIsNone(recs["notion:h-lee"][1])
        self.assertIn("인증은 OAuth로 확정", recs["notion:r1"][0])  # DB 행 본문까지
        self.assertIn("요약: OAuth 결정", recs["notion:r1"][0])
        self.assertIn("캐시 전략 리뷰", recs["notion:m2pg"][0])
        self.assertTrue(recs["notion:h-kim"][0].startswith("[진행 기록] 알파팀 정보 > 팀원별 진행상황 > 김팀원"))

    def test_unchanged_workspace_costs_only_metadata_checks(self) -> None:
        self.sync()
        calls = len(self.llm.calls)
        stats = self.sync()
        self.assertEqual((stats["structure_changed"], stats["records_new"], stats["judged"]), ([], 0, 0))
        self.assertEqual(len(self.llm.calls), calls)  # 안 바뀌면 LLM도 다시 안 부른다
        self.assertTrue(all(c.startswith(("page:", "db:", "query:")) for c in self.fake.calls))

    def test_content_edit_rereads_only_that_part_and_supersedes_draft(self) -> None:
        self.sync()
        self.fake.dbs["db-meeting"][0] = row("r1", "11/1 주간회의", "2026-11-01T02:00:00Z", body="OAuth 확정, 담당 김팀원", 요약="OAuth 결정")
        stats = self.sync()
        self.assertEqual((stats["structure_changed"], stats["records_new"], stats["superseded"]), ([], 1, 1))
        self.assertEqual([h for loc, _, h in self.records() if loc == "notion:r1"], ["superseded", None])

    def test_inline_edit_changes_page_time_but_not_structure(self) -> None:
        self.sync()
        root = self.fake.pages["root"]
        root["edited"] = "2026-11-02T00:00:00Z"
        root["blocks"][3] = para("p-kim", "로그인 API 구현 완료 #4")
        stats = self.sync()
        self.assertEqual(stats["structure_changed"], [])
        self.assertIn("구현 완료", [t for loc, t, h in self.records() if loc == "notion:h-kim" and h is None][-1])

    def test_removed_subpage_deactivates_its_sections(self) -> None:
        self.sync()
        root = self.fake.pages["root"]
        root["edited"] = "2026-11-02T00:00:00Z"
        root["blocks"] = [b for b in root["blocks"] if b["id"] != "pg-mentor"]
        stats = self.sync()
        self.assertTrue(any("1회차" in p for p in stats["sections_removed"]))
        self.assertEqual(self.by_title()["1회차"]["status"], "inactive")
        self.assertTrue(any(loc == "notion:m2pg" for loc, _, _ in self.records()))  # 쌓인 원문은 남음

    def test_grandchild_page_survives_a_rescan_of_the_root(self) -> None:
        self.fake.pages["pg-mentor"]["blocks"].insert(0, child_page("pg-guide", "멘토링 가이드"))
        self.fake.pages["pg-guide"] = {"title": "멘토링 가이드", "edited": "2026-11-01T00:00:00Z",
                                       "blocks": [heading("g1", "질문 목록", 1), para("g1p", "인증 질문")]}
        self.sync()
        root = self.fake.pages["root"]
        root["edited"] = "2026-11-02T00:00:00Z"
        root["blocks"].append(heading("h-new", "주간 보고", 1))
        self.sync()
        self.assertEqual(self.by_title()["질문 목록"]["status"], "active")
        mentor = self.fake.pages["pg-mentor"]
        mentor["edited"] = "2026-11-03T00:00:00Z"
        mentor["blocks"] = [b for b in mentor["blocks"] if b["id"] != "pg-guide"]
        self.sync()
        self.assertEqual(self.by_title()["질문 목록"]["status"], "inactive")


class MapCommandTests(unittest.TestCase):
    """기본은 자동, 사람은 섹션별 역할을 보고 바꾸기만 한다."""

    def setUp(self) -> None:
        import io
        from contextlib import redirect_stdout

        from worklog.__main__ import main

        self.tmp = tempfile.TemporaryDirectory()
        self.store = Path(self.tmp.name) / "w.sqlite3"
        notion_map.sync(self.store, SCOPE, "root", ClientView(team_workspace()), classify=FakeClassifier())

        def run(*argv: str) -> tuple[int, str]:
            out = io.StringIO()
            with redirect_stdout(out):
                code = main(["--store", str(self.store), *argv])
            return code, out.getvalue()

        self.run_cli = run

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_map_lists_each_section_with_its_role(self) -> None:
        code, out = self.run_cli("map", "--scope", SCOPE)
        self.assertEqual(code, 0)
        self.assertIn("[진행 기록] 알파팀 정보 > 팀원별 진행상황 > 김팀원  · LLM", out)
        self.assertIn("[분석 제외] 알파팀 정보 > 공지사항  · LLM", out)

    def test_change_role_by_section_name_in_korean(self) -> None:
        code, out = self.run_cli("map-set", "--scope", SCOPE, "공지사항", "진행", "--actor", "lead")
        self.assertEqual(code, 0)
        self.assertIn("알파팀 정보 > 공지사항: 분석 제외 → 진행 기록", out)
        _, table = self.run_cli("map", "--scope", SCOPE)
        self.assertIn("[진행 기록] 알파팀 정보 > 공지사항  · lead", table)

    def test_parent_change_carries_inheriting_children(self) -> None:
        sections.set_role(self.store, "h-kim", "reference", "x")  # 사람이 정한 건 상속 대상이 아님
        notion_map.sync  # noqa: B018 - 지도는 그대로
        code, out = self.run_cli("map-set", "--scope", SCOPE, "팀원별 진행상황", "회의", "--actor", "lead")
        self.assertEqual(code, 0)
        roles = {r["section"].split(" > ")[-1]: r["role"] for r in sections.role_table(self.store, SCOPE)}
        self.assertEqual(roles["팀원별 진행상황"], "meeting")
        self.assertEqual(roles["김팀원"], "reference")

    def test_ambiguous_or_unknown_names_are_refused(self) -> None:
        sections.save_section(self.store, SCOPE, {"id": "dup", "page_id": "root", "path": "알파팀 정보 > 기타 > 김팀원",
                                                  "title": "김팀원", "role": "progress", "role_source": "llm",
                                                  "layout": {"inline": True, "pages": [], "databases": [], "unreadable": []}})
        code, _ = self.run_cli("map-set", "--scope", SCOPE, "김팀원", "보관", "--actor", "lead")
        self.assertEqual(code, 2)
        code, _ = self.run_cli("map-set", "--scope", SCOPE, "팀원별 진행상황 > 김팀원", "보관", "--actor", "lead")
        self.assertEqual(code, 0)
        self.assertEqual(self.run_cli("map-set", "--scope", SCOPE, "진행상황 > 김팀원", "보관", "--actor", "lead")[0], 4)
        self.assertEqual(self.run_cli("map-set", "--scope", SCOPE, "없는 섹션", "보관", "--actor", "lead")[0], 4)
        self.assertEqual(self.run_cli("map-set", "--scope", SCOPE, "공지사항", "아무거나", "--actor", "lead")[0], 2)


if __name__ == "__main__":
    unittest.main()
