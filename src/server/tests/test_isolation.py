"""테스트가 실제 저장소를 가리키지 않는지 확인한다(tests/isolation.py)."""

from __future__ import annotations

import isolation  # noqa: F401,I001  실제 저장소·키를 쓰지 않게(맨 먼저)

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import worklog_bridge  # noqa: E402


class IsolationTests(unittest.TestCase):
    def test_tests_never_point_at_the_real_store(self) -> None:
        self.assertNotEqual(Path(worklog_bridge.WORKLOG_STORE).resolve(), isolation.REAL_STORE.resolve())
        self.assertEqual(Path(worklog_bridge.WORKLOG_STORE), isolation.TEST_STORE)


if __name__ == "__main__":
    unittest.main()
