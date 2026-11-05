"""테스트가 실제 저장소를 가리키지 않는지 확인한다(tests/isolation.py)."""

from __future__ import annotations

import isolation  # noqa: F401,I001  실제 저장소·키를 쓰지 않게(맨 먼저)

import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import worklog_bridge  # noqa: E402
from worklog import db  # noqa: E402


class IsolationTests(unittest.TestCase):
    def test_tests_never_point_at_the_real_store(self) -> None:
        self.assertNotEqual(Path(worklog_bridge.WORKLOG_STORE).resolve(), isolation.REAL_STORE.resolve())
        self.assertEqual(Path(worklog_bridge.WORKLOG_STORE), isolation.TEST_STORE)

    def test_tests_have_no_real_llm_keys_even_after_dotenv(self) -> None:
        import llm_queue
        import main  # noqa: F401  main은 불러올 때 .env를 읽는다

        self.assertEqual(llm_queue.available(), [])
        self.assertFalse(worklog_bridge.GROQ_API_KEY)

    def test_test_mode_blocks_database_connections_to_the_protected_store(self) -> None:
        with TemporaryDirectory() as temp:
            protected = Path(temp) / "protected.sqlite3"
            connection = None
            with patch.dict("os.environ", {"WORKLOG_TEST_MODE": "1", "WORKLOG_PROTECTED_STORE": str(protected)}):
                try:
                    with self.assertRaisesRegex(RuntimeError, "test isolation"):
                        connection = db.connect(protected)
                finally:
                    if connection is not None:
                        connection.close()
            self.assertFalse(protected.exists())

    def test_llm_usage_metering_cannot_write_to_a_late_selected_protected_store(self) -> None:
        with TemporaryDirectory() as temp:
            protected = Path(temp) / "protected.sqlite3"
            stale = Path(temp) / "stale.sqlite3"
            provider = SimpleNamespace(name="test", url="https://example.invalid", key="fake", model="fake")
            response = MagicMock(status_code=200, headers={})
            response.json.return_value = {
                "choices": [{"message": {"content": "{}"}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7},
            }
            response.raise_for_status.return_value = None
            settings = {"WORKLOG_TEST_MODE": "1", "WORKLOG_STORE": str(protected),
                        "WORKLOG_PROTECTED_STORE": str(protected)}
            with patch.dict("os.environ", settings), \
                    patch.object(worklog_bridge, "WORKLOG_STORE", stale), \
                    patch.object(worklog_bridge.llm_queue, "available", return_value=[provider]), \
                    patch("httpx.Client.post", return_value=response):
                with self.assertRaisesRegex(RuntimeError, "test isolation"):
                    worklog_bridge.lead_call("coordination", {})
            self.assertFalse(protected.exists())
            self.assertFalse(stale.exists())


if __name__ == "__main__":
    unittest.main()
