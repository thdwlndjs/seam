"""Notion 호출 관문과 긴 요청 처리 테스트. 실행: server 폴더에서 python -m unittest discover -s tests"""

from __future__ import annotations

import isolation  # noqa: F401,I001  실제 저장소·키를 쓰지 않게(맨 먼저)

import asyncio
import os
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("BOT_SERVER_SECRET", "test-secret")
os.environ["WORKLOG_LOOP"] = "0"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import notion_read  # noqa: E402


class Concurrency:
    def __init__(self, hold: float = 0.05) -> None:
        self.hold, self.now, self.peak, self.lock = hold, 0, 0, threading.Lock()

    def __call__(self, *args, **kwargs):
        with self.lock:
            self.now += 1
            self.peak = max(self.peak, self.now)
        time.sleep(self.hold)
        with self.lock:
            self.now -= 1
        return {"results": [], "has_more": False}


class GateTests(unittest.TestCase):
    def gate(self, rate=1000.0, background=1000.0, slots=3) -> notion_read._Gate:
        with patch.object(notion_read, "NOTION_RATE", rate), patch.object(notion_read, "NOTION_BACKGROUND_RATE", background), \
                patch.object(notion_read, "NOTION_CONCURRENCY", slots):
            return notion_read._Gate()

    def test_rate_limiter_spreads_calls(self) -> None:
        limiter = notion_read._RateLimiter(rate=10, burst=2)
        start = time.monotonic()
        for _ in range(6):
            limiter.acquire()
        self.assertGreaterEqual(time.monotonic() - start, 0.35)  # 2개는 바로, 나머지 4개는 0.1초 간격

    def test_concurrent_calls_are_capped_per_token(self) -> None:
        gate, fn = self.gate(slots=2), Concurrency()
        threads = [threading.Thread(target=gate.call, args=(fn, False)) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(fn.peak, 2)

    def test_background_gets_only_its_share(self) -> None:
        gate = self.gate(background=5)
        start = time.monotonic()
        for _ in range(4):
            gate.call(lambda: None, False)
        foreground = time.monotonic() - start
        start = time.monotonic()
        for _ in range(4):
            gate.call(lambda: None, True)
        background = time.monotonic() - start
        self.assertLess(foreground, 0.1)
        self.assertGreaterEqual(background, 0.5)  # 초당 5회: 처음 1회 뒤 3회는 0.2초 간격

    def test_same_token_shares_one_gate(self) -> None:
        a = notion_read.notion_client_for("secret_same")
        b = notion_read.notion_client_for("secret_same", background=True)
        c = notion_read.notion_client_for("secret_other")
        self.assertIs(a._gate, b._gate)
        self.assertIsNot(a._gate, c._gate)

    def test_real_client_methods_go_through_the_gate(self) -> None:
        client = notion_read.notion_client_for("secret_wrap")
        calls = []
        with patch.object(client._gate, "call", side_effect=lambda fn, bg, *a, **k: calls.append((fn.__name__, bg, a)) or {}):
            client.blocks.children.list("block-1", start_cursor=None)
            client.pages.retrieve("page-1")
            client.data_sources.query("ds-1")
        self.assertEqual([c[0] for c in calls], ["list", "retrieve", "query"])
        self.assertEqual(calls[0][2], ("block-1",))

    def test_parallel_reads_are_bounded_per_level(self) -> None:
        fn = Concurrency(hold=0.02)
        notion_read._map_parallel(lambda item: [fn()], [{} for _ in range(12)])
        self.assertLessEqual(fn.peak, notion_read.PARALLEL_READS)


class LongTurnTests(unittest.TestCase):
    def setUp(self) -> None:
        from fastapi.testclient import TestClient

        import main

        self.main = main
        self.client = TestClient(main.app)

    def post(self) -> str:
        body = {"user": {"aad_id": "u1"}, "text": "2"}
        return self.client.post("/api/turn", json=body, headers={"X-Bot-Secret": "test-secret"}).text

    def test_slow_turn_sends_keepalive_then_gives_up_with_a_message(self) -> None:
        async def slow(turn):
            await asyncio.sleep(2)
            await turn.send("늦은 답")

        with patch.object(self.main, "handle_turn", slow), patch.object(self.main, "KEEPALIVE_SECONDS", 0.05), \
                patch.object(self.main, "TURN_DEADLINE_SECONDS", 0.3):
            text = self.post()
        self.assertIn('"ping"', text)
        self.assertIn("응답이 늦어", text)
        self.assertNotIn("늦은 답", text)

    def test_normal_turn_has_no_ping(self) -> None:
        async def quick(turn):
            await turn.send("바로 답")

        with patch.object(self.main, "handle_turn", quick):
            text = self.post()
        self.assertNotIn("ping", text)
        self.assertIn("바로 답", text)


if __name__ == "__main__":
    unittest.main()
