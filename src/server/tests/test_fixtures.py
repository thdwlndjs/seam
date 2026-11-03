"""Notion fixture 점검: 커밋하는 fixture에 비밀값이 없고, md 최상위 줄이 블록 목록과 하나씩 맞는다.

md와 블록 목록이 1:1로 맞는다는 것은 Phase 1 블록 대조(docs/v1.1/spec.md P1-2)의 전제다.
실제 녹화본(tests/fixtures/notion_local/, 커밋하지 않음)이 있으면 그것으로도 확인한다.
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SECRET = re.compile(r"\b(secret_|ntn_|ghp_|github_pat_|gsk_|sk-|proj_|xox[abp]-)[A-Za-z0-9_\-]{6,}")
PASSWORD = re.compile(r"(비밀번호|password)\s*[:：]\s*(?!\[REDACTED\])\S+", re.IGNORECASE)


def top_level_lines(md: str) -> list[str]:
    """최상위 블록마다 그 블록의 첫 줄 하나.

    - 중첩은 탭으로만 표시된다. 공백으로 시작하는 줄(` \\[클로바노트공유\\]`)도 최상위 단락이다.
    - 표 안(<table> … </table>)의 <tr>·<td> 줄은 들여쓰기가 없어도 그 표에 속한다. 최상위 <table> 줄은 블록 하나다.
    - 들여쓰기 없는 닫는 태그(`</callout>`)는 앞 블록에 속한다.
    """
    lines, in_table = [], False
    # splitlines()는   같은 문자에서도 끊으므로 쓰지 않는다. CRLF로 체크아웃된 경우만 맞춘다.
    for line in md.replace("\r\n", "\n").split("\n"):
        stripped = line.strip()
        if in_table:
            in_table = not stripped.startswith("</table")
            continue
        if stripped.startswith("<table"):
            in_table = True
        if not line or line.startswith("\t") or stripped.startswith("</"):
            continue
        lines.append(line)
    return lines


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
            lines = top_level_lines(md_path.read_text(encoding="utf-8"))
            blocks = json.loads(blocks_path.read_text(encoding="utf-8"))
            self.assertEqual(len(lines), len(blocks), md_path.name)
            for line, block in zip(lines, blocks):
                if block["type"] == "divider":
                    self.assertEqual(line, "---", md_path.name)
                elif block["type"] == "paragraph" and not block["text"]:
                    self.assertEqual(line, "<empty-block/>", md_path.name)


if __name__ == "__main__":
    unittest.main()
