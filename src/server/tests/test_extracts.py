"""부분별 추출·Groq 대기열·메뉴 2번 테스트. 문서: docs/v1.1/spec.md P1-4, P1-8, P1-9, Phase 2 수락 기준."""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

os.environ.setdefault("BOT_SERVER_SECRET", "test-secret")
os.environ["WORKLOG_LOOP"] = "0"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import groq_queue  # noqa: E402
import main  # noqa: E402
import metrics  # noqa: E402
import notion_map  # noqa: E402
import worklog_bridge  # noqa: E402
from test_notion_map import FakeClassifier, MarkdownClientView, meeting_kinds, team_workspace  # noqa: E402
from test_parts import SAMPLE_BLOCKS, SAMPLE_MD, split_classifier  # noqa: E402
from worklog import parts  # noqa: E402

SCOPE = "team:alpha"
V1, V2 = "test-v1", "test-v2"


class FakeExtractor:
    """부분 글에 있는 구절로 항목을 만든다. 원문에 없는 인용 하나를 일부러 섞는다."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, payload: dict) -> dict:
        self.calls.append(payload)
        text = payload["text"]
        items = []
        if "AI 어디에 사용?" in text:
            items.append({"kind": "open", "title": "AI를 어디에 쓸지", "quote": "AI 어디에 사용?", "confidence": 0.9})
        if "식상한거 하지말고" in text:
            items.append({"kind": "feedback", "title": "흥미를 끌 주제로", "quote": "식상한거 하지말고 흥미를 끌만한 주제로"})
        if "책 등록은 어떻게?" in text:
            items.append({"kind": "task", "title": "책 등록 방법 정리", "owner": "김팀원", "due": "2026-11-10",
                          "role_hint": "dev", "quote": "책 등록은 어떻게?"})
        items.append({"kind": "decision", "title": "없는 말", "quote": "회의록에 없는 문장"})  # 버려져야 한다
        return {"items": items, "usage": {"prompt_tokens": 100, "completion_tokens": 20}, "model": "fake"}


class ExtractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Path(self.tmp.name) / "w.sqlite3"
        built = parts.build_unit_parts("unit-1", "회의록 > 1차", SAMPLE_MD.replace("→ 식상한거", "→ 식상한거")
                                       .replace("→ 스마트 책장", "→ 식상한거 하지말고 흥미를 끌만한 주제로"), SAMPLE_BLOCKS)
        parts.save_unit_parts(self.store, SCOPE, "notion", "unit-1", "sec-1", built, last_edited="2026-11-01T00:00:00Z")
        parts.judge_pending(self.store, SCOPE, split_classifier(kinds={"→ 정기 배송 쪽으로 가면 비즈니스적으로 설명하기는 좋을듯": "meeting"}))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_only_meeting_parts_are_extracted_and_bad_quotes_are_dropped(self) -> None:  # A12, A22
        extractor = FakeExtractor()
        done = parts.extract_pending(self.store, SCOPE, extractor, V1, budget_tokens=None)
        self.assertEqual(done, 6)  # 회의 메모 6개. 떼어 낸 '구체화' 문서는 보내지 않는다
        self.assertFalse(any("꽂힌 책을 찾지 않고" in c["text"] for c in extractor.calls))
        items = parts.meeting_items(self.store, SCOPE, V1)
        titles = {i["title"] for i in items}
        self.assertIn("AI를 어디에 쓸지", titles)
        self.assertIn("흥미를 끌 주제로", titles)
        self.assertNotIn("없는 말", titles)  # 원문에 없는 인용은 버린다
        task = next(i for i in items if i["kind"] == "task")
        self.assertEqual((task["owner"], task["due"], task["role_hint"]), ("김팀원", "2026-11-10", "dev"))

    def test_same_content_and_version_are_not_extracted_again(self) -> None:  # A5
        extractor = FakeExtractor()
        parts.extract_pending(self.store, SCOPE, extractor, V1, budget_tokens=None)
        calls = len(extractor.calls)
        self.assertEqual(parts.extract_pending(self.store, SCOPE, extractor, V1, budget_tokens=None), 0)
        self.assertEqual(len(extractor.calls), calls)  # 재시작해도 저장소에 남아 다시 보내지 않는다
        self.assertEqual(parts.extract_pending(self.store, SCOPE, extractor, V2, budget_tokens=None), 6)  # 새 판이면 다시

    def test_same_item_from_two_parts_is_merged_with_both_sources(self) -> None:
        def same_everywhere(payload: dict) -> dict:
            quote = payload["text"].split("\n")[0].strip()
            return {"items": [{"kind": "open", "title": "AI 활용처", "quote": quote}], "usage": {}, "model": "fake"}

        parts.extract_pending(self.store, SCOPE, same_everywhere, V1, budget_tokens=None)
        merged = [i for i in parts.meeting_items(self.store, SCOPE, V1) if i["title"] == "AI 활용처"]
        self.assertEqual(len(merged), 1)
        self.assertEqual(len(merged[0]["sources"]), 6)

    def test_no_llm_or_busy_queue_waits_for_the_next_round(self) -> None:
        self.assertEqual(parts.extract_pending(self.store, SCOPE, lambda p: {}, V1), 0)

        def busy(payload: dict) -> dict:
            raise groq_queue.Busy("minute", 30)

        self.assertEqual(parts.extract_pending(self.store, SCOPE, busy, V1), 0)
        self.assertEqual(parts.pending_count(self.store, SCOPE, V1), 6)

    def test_big_parts_are_sent_in_line_chunks(self) -> None:
        big_md = "1. 긴 회의\n" + "\n".join(f"\t- 논의 {i}: " + "가나다라마바사 " * 30 for i in range(80))
        parts.save_unit_parts(self.store, SCOPE, "notion", "unit-2", "sec-1", parts.build_unit_parts("unit-2", "회의록 > 2차", big_md, None))
        parts.judge_pending(self.store, SCOPE, meeting_kinds_for_all)
        extractor = FakeExtractor()
        parts.extract_pending(self.store, SCOPE, extractor, V1, budget_tokens=None)
        big_calls = [c for c in extractor.calls if c["path"].endswith("긴 회의")]
        self.assertGreater(len(big_calls), 1)
        self.assertTrue(all(parts.estimate_tokens(c["text"]) <= parts.EXTRACT_BUDGET_TOKENS for c in big_calls))


def meeting_kinds_for_all(page: dict) -> dict:
    return {"parts": [{"part_id": p["part_id"], "kind": "meeting", "confidence": 0.9, "split": []} for p in page["parts"]]}


class QueueTests(unittest.TestCase):  # A21
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Path(self.tmp.name) / "w.sqlite3"
        groq_queue.reset_for_tests()

    def tearDown(self) -> None:
        groq_queue.reset_for_tests()
        self.tmp.cleanup()

    def test_user_calls_are_never_blocked_but_background_waits_for_the_minute_reset(self) -> None:
        groq_queue.record(self.store, groq_queue.USER, {"x-ratelimit-remaining-tokens": "300",
                                                        "x-ratelimit-reset-tokens": "7.5s"}, 200, 1000)
        groq_queue.check(groq_queue.USER, 5000, self.store)  # 사람이 기다리는 호출은 그대로
        with self.assertRaises(groq_queue.Busy) as busy:
            groq_queue.check(groq_queue.BACKGROUND, 1000, self.store)
        self.assertEqual(busy.exception.reason, "minute")
        self.assertLessEqual(busy.exception.wait_seconds, 7.5)
        groq_queue.check(groq_queue.BACKGROUND, 200, self.store)  # 남은 양 안이면 된다

    def test_429_blocks_background_until_retry_after(self) -> None:
        groq_queue.record(self.store, groq_queue.BACKGROUND, {"retry-after": "30"}, 429, 0)
        with self.assertRaises(groq_queue.Busy) as busy:
            groq_queue.check(groq_queue.BACKGROUND, 10, self.store)
        self.assertGreater(busy.exception.wait_seconds, 25)
        self.assertIn("초 뒤 이어서 정리해요", groq_queue.busy_message(busy.exception))

    def test_background_keeps_to_its_daily_share(self) -> None:
        share = int(groq_queue.DAILY_TOKENS * groq_queue.BACKGROUND_SHARE)
        groq_queue.record(self.store, groq_queue.USER, {}, 200, share - 500)
        groq_queue.check(groq_queue.BACKGROUND, 400, self.store)
        with self.assertRaises(groq_queue.Busy) as busy:
            groq_queue.check(groq_queue.BACKGROUND, 600, self.store)
        self.assertEqual(busy.exception.reason, "daily")
        self.assertIn("내일 다시 정리돼요", groq_queue.busy_message(busy.exception))
        groq_queue.check(groq_queue.USER, 10_000, self.store)  # 사람이 누른 것은 한도 끝까지
        self.assertEqual(groq_queue.daily_usage(self.store)["tokens"], share - 500)

    def test_reset_durations_are_parsed(self) -> None:
        self.assertEqual(groq_queue.parse_duration("1m2.5s"), 62.5)
        self.assertEqual(groq_queue.parse_duration("120ms"), 0.12)
        self.assertEqual(groq_queue.parse_duration(None), 0.0)


class MenuTests(unittest.TestCase):
    ROW_MD = ("1. 인증\n\t- OAuth로 확정\n\t- 책 등록은 어떻게?\n<empty-block/>\n"
              "→ 식상한거 하지말고 흥미를 끌만한 주제로\n→ AI 어디에 사용?")

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Path(self.tmp.name) / "w.sqlite3"
        self.patch = patch.object(worklog_bridge, "WORKLOG_STORE", self.store)
        self.patch.start()
        fake = team_workspace()
        notion_map.sync_roots(self.store, "team:T1", ["root"], MarkdownClientView(fake, {"r1": self.ROW_MD}),
                              classify=FakeClassifier(), classify_parts=meeting_kinds)
        self.config = {"notion_page_id": "root", "notion_token": "t", "github_token": "g", "github_repo": "o/r"}

    def tearDown(self) -> None:
        self.patch.stop()
        self.tmp.cleanup()

    def menu2(self) -> tuple[str, dict]:
        with patch.object(main, "_github_get_cached", new=AsyncMock(return_value=[])), \
                patch.object(main, "_get_commit_touched_issues", new=AsyncMock(return_value=set())), \
                patch.object(main, "notion_client_for", side_effect=AssertionError("Notion을 부르면 안 된다")), \
                patch.object(main, "GROQ_API_KEY", "k"), \
                patch("httpx.AsyncClient.post", side_effect=AssertionError("메뉴에서 Groq를 부르면 안 된다")), \
                metrics.measure("menu", "team:T1", "2") as meter:
            text, _ = asyncio.run(main.fetch_team_status(self.config, "team:T1"))
        return text, meter.counts

    def test_menu2_reads_stored_extracts_without_calling_groq(self) -> None:  # A6
        parts.extract_pending(self.store, "team:T1", FakeExtractor(), worklog_bridge.EXTRACT_PROMPT_VERSION, budget_tokens=None)
        text, counts = self.menu2()
        self.assertEqual(counts["groq_calls"], 0)
        self.assertIn("(회의록 기준, 확인 전)", text)
        self.assertIn("💬 멘토링 피드백", text)
        self.assertIn("흥미를 끌 주제로", text)
        self.assertIn("AI를 어디에 쓸지", text)
        self.assertIn("김팀원: 책 등록 방법 정리", text)
        self.assertNotIn("정리 중", text)

    def test_menu2_shows_raw_notes_and_progress_before_extraction(self) -> None:
        text, counts = self.menu2()
        self.assertEqual(counts["groq_calls"], 0)
        self.assertIn("회의록 정리 중", text)
        self.assertIn("건 정리 중", text)

    def test_menu1_lists_documents_with_their_one_line_summary(self) -> None:
        lines = main._document_lines([{"path": "회의록 > 1차 > 구체화", "summary": "스마트 옷장 기획서, 9/28 기준",
                                       "page_id": "0551-1be9"}])
        self.assertEqual(lines[0], "📄 주요 문서")
        self.assertEqual(lines[1], "- 구체화 — 스마트 옷장 기획서, 9/28 기준 (https://www.notion.so/05511be9)")


if __name__ == "__main__":
    unittest.main()
