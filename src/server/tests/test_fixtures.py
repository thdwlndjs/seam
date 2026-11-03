"""Notion fixture 점검: 커밋하는 fixture에 비밀값이 없고, md 최상위 줄이 블록 목록과 하나씩 맞는다.

md와 블록 목록이 1:1로 맞는다는 것은 Phase 1 블록 대조(docs/v1.1/spec.md P1-2)의 전제다.
실제 녹화본(tests/fixtures/notion_local/, 커밋하지 않음)이 있으면 그것으로도 확인한다.
"""

from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from worklog import parts  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SECRET = re.compile(r"\b(secret_|ntn_|ghp_|github_pat_|gsk_|sk-|proj_|xox[abp]-)[A-Za-z0-9_\-]{6,}")
PASSWORD = re.compile(r"(비밀번호|password)\s*[:：]\s*(?!\[REDACTED\])\S+", re.IGNORECASE)


def top_level_lines(md: str) -> list[str]:
    """최상위 블록마다 그 블록의 첫 줄 하나. 규칙은 worklog.parts.top_level_blocks(Phase 1에서 옮김)."""
    return [block.first for block in parts.top_level_blocks(md)]


def recorded_pairs(folder: Path) -> list[tuple[Path, Path]]:
    return [(md, md.with_suffix(".blocks.json")) for md in sorted(folder.glob("*.md"))
            if md.with_suffix(".blocks.json").exists()]


class FixtureTests(unittest.TestCase):
    def test_committed_fixtures_have_no_secrets(self) -> None:
        for path in (FIXTURES / "notion").glob("*"):
            text = path.read_text(encoding="utf-8")
            self.assertIsNone(SECRET.search(text), path.name)
            self.assertIsNone(PASSWORD.search(text), path.name)

    def test_sample_markdown_lines_match_top_blocks_one_to_one(self) -> None:
        self._assert_one_to_one(recorded_pairs(FIXTURES / "notion"))

    def test_local_recordings_match_top_blocks_one_to_one(self) -> None:
        pairs = recorded_pairs(FIXTURES / "notion_local")
        if not pairs:
            self.skipTest("실제 녹화본 없음(scripts/record_notion_fixtures.py로 만든다)")
        self._assert_one_to_one(pairs)

    def _assert_one_to_one(self, pairs: list[tuple[Path, Path]]) -> None:
        self.assertTrue(pairs)
        for md_path, blocks_path in pairs:
            md = md_path.read_text(encoding="utf-8")
            lines = top_level_lines(md)
            blocks = json.loads(blocks_path.read_text(encoding="utf-8"))
            self.assertEqual(len(lines), len(blocks), md_path.name)
            for line, block in zip(lines, blocks):
                if block["type"] == "divider":
                    self.assertEqual(line, "---", md_path.name)
                elif block["type"] == "paragraph" and not block["text"]:
                    self.assertEqual(line, "<empty-block/>", md_path.name)
            # Phase 1 블록 대조(종류까지 맞춰 보는 것)도 실제 녹화본에서 통과해야 한다.
            self.assertIsNotNone(parts.align_blocks(parts.top_level_blocks(md), blocks), md_path.name)


if __name__ == "__main__":
    unittest.main()
