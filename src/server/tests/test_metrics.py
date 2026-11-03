"""계측(metric 한 줄) 테스트. 문서: docs/v1.1/spec.md "계측"."""

from __future__ import annotations

import isolation  # noqa: F401,I001  실제 저장소·키를 쓰지 않게(맨 먼저)

import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

os.environ.setdefault("BOT_SERVER_SECRET", "test-secret")
os.environ["WORKLOG_LOOP"] = "0"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402
import metrics  # noqa: E402
import notion_read  # noqa: E402
import worklog_bridge  # noqa: E402

HEADERS = {"X-Bot-Secret": "test-secret"}
SECRET_PATTERN = re.compile(r"(secret_|ntn_|ghp_|gsk_)\w+")


class MeterTests(unittest.TestCase):
    def test_counts_notion_calls_through_the_gate_including_pool_threads(self) -> None:
        gate = notion_read._Gate()
        with self.assertLogs("metrics", "INFO") as logs, metrics.measure("menu", "team:T1", "1") as meter:
            gate.call(lambda: None, False)
            notion_read._map_parallel(lambda item: [gate.call(lambda: item, False)], [{}, {}, {}])
        self.assertEqual(meter.counts["notion_calls"], 4)
        self.assertIn("notion_calls=4", logs.output[0])

    def test_counts_groq_calls_and_tokens(self) -> None:
        with self.assertLogs("metrics", "INFO") as logs, metrics.measure("background") as meter:
            metrics.count_groq({"total_tokens": 120})
            metrics.count_groq(None)
        self.assertEqual((meter.counts["groq_calls"], meter.counts["groq_tokens"]), (2, 120))
        self.assertRegex(logs.output[0], r"metric kind=background team=- menu=- .*groq_calls=2 groq_tokens=120 .*elapsed_ms=\d+")

    def test_calls_outside_a_measured_block_are_ignored(self) -> None:
        metrics.count_notion()
        metrics.count_groq({"total_tokens": 5})  # 예외 없이 지나간다

    def test_groq_json_counts_one_call(self) -> None:
        class FakeResponse:
            def json(self):
                return {"choices": [{"message": {"content": "{}"}}], "usage": {"total_tokens": 7}}

            def raise_for_status(self):
                return None

        with patch("httpx.Client.post", return_value=FakeResponse()), \
                self.assertLogs("metrics", "INFO"), metrics.measure("background") as meter:
            worklog_bridge._groq_json("prompt", {})
        self.assertEqual((meter.counts["groq_calls"], meter.counts["groq_tokens"]), (1, 7))


class MenuMetricTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.patches = [
            patch.object(worklog_bridge, "WORKLOG_STORE", root / "worklog.sqlite3"),
            patch.object(main, "TEAM_CONFIG_PATH", str(root / "team_configs.json")),
            patch.object(main, "MEMBER_PROFILE_PATH", str(root / "member_profiles.json")),
        ]
        for p in self.patches:
            p.start()
        self.client = TestClient(main.app)

    def tearDown(self) -> None:
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def test_one_menu_turn_logs_exactly_one_metric_line_without_secrets(self) -> None:
        main._save_team_configs({"team:T1": {"notion_page_id": "root", "notion_token": "secret_abc",
                                             "github_token": "ghp_abc", "github_repo": "o/r"}})
        body = {"user": {"aad_id": "u1", "name": "김팀원"}, "team": {"id": "T1", "name": "알파"},
                "text": "", "value": {"action": "menu", "choice": "1"}}
        with self.assertLogs("metrics", "INFO") as logs, \
                patch.object(main, "fetch_project_info", new=AsyncMock(return_value=None)):
            resp = self.client.post("/api/turn", json=body, headers=HEADERS)
        self.assertEqual(resp.status_code, 200)
        lines = [line for line in logs.output if "metric " in line]
        self.assertEqual(len(lines), 1)
        self.assertIn("kind=menu team=team:T1 menu=1", lines[0])
        self.assertIsNone(SECRET_PATTERN.search(lines[0]))


if __name__ == "__main__":
    unittest.main()
