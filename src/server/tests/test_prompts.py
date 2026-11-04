"""판단 지침(Markdown) 로더 테스트. 문서: docs/implementation/v2.1/plan.md 2.4, Phase 1."""

from __future__ import annotations

import isolation  # noqa: F401,I001  실제 저장소·키를 쓰지 않게(맨 먼저)

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import prompts  # noqa: E402
import worklog_bridge  # noqa: E402


def touch_later(path: Path) -> None:
    """같은 초 안에 고쳐도 mtime이 달라지게 한다."""
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 2_000_000_000))


class LoaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        (self.dir / "common").mkdir()
        (self.dir / "common" / "rule.md").write_text("공통 규칙 한 줄\n", encoding="utf-8")
        (self.dir / "main.md").write_text("---\nversion: demo-1\n---\n첫 줄\n@include common/rule.md\n마지막 줄\n", encoding="utf-8")
        self.patch = patch.object(prompts, "PROMPT_DIR", self.dir)
        self.patch.start()
        prompts.clear_cache()

    def tearDown(self) -> None:
        self.patch.stop()
        prompts.clear_cache()
        self.tmp.cleanup()

    def test_front_matter_is_removed_and_include_is_expanded(self) -> None:
        loaded = prompts.load("main")
        self.assertEqual(loaded.text, "첫 줄\n공통 규칙 한 줄\n마지막 줄")
        self.assertNotIn("version:", loaded.text)
        self.assertTrue(loaded.version.startswith("demo-1+"))  # 본문 해시를 모르는 판은 해시를 붙인다

    def test_unchanged_files_come_from_the_cache(self) -> None:
        self.assertIs(prompts.load("main"), prompts.load("main"))

    def test_editing_the_file_or_an_included_file_changes_the_version(self) -> None:
        first = prompts.load("main")
        (self.dir / "main.md").write_text("---\nversion: demo-1\n---\n첫 줄 고침\n@include common/rule.md\n", encoding="utf-8")
        touch_later(self.dir / "main.md")
        second = prompts.load("main")
        self.assertNotEqual(first.version, second.version)
        self.assertIn("첫 줄 고침", second.text)
        (self.dir / "common" / "rule.md").write_text("공통 규칙 바뀜\n", encoding="utf-8")  # 포함한 파일만 고쳐도
        touch_later(self.dir / "common" / "rule.md")
        third = prompts.load("main")
        self.assertNotEqual(second.version, third.version)
        self.assertIn("공통 규칙 바뀜", third.text)

    def test_baseline_hash_keeps_the_old_version_name_until_the_text_changes(self) -> None:
        body = "옮기기만 한 지침"
        (self.dir / "moved.md").write_text(f"---\nversion: old-3\nbody_sha256: {prompts.sha256_text(body)}\n---\n{body}\n", encoding="utf-8")
        self.assertEqual(prompts.load("moved").version, "old-3")  # 내용이 그대로면 기존 캐시 키 유지
        (self.dir / "moved.md").write_text(f"---\nversion: old-3\nbody_sha256: {prompts.sha256_text(body)}\n---\n{body} 고침\n", encoding="utf-8")
        touch_later(self.dir / "moved.md")
        self.assertTrue(prompts.load("moved").version.startswith("old-3+"))

    def test_bad_includes_are_rejected(self) -> None:
        (self.dir / "a.md").write_text("@include b.md\n", encoding="utf-8")
        (self.dir / "b.md").write_text("@include a.md\n", encoding="utf-8")
        with self.assertRaises(prompts.PromptError):  # 돌고 도는 포함
            prompts.load("a")
        (self.dir / "c.md").write_text("@include nothing.md\n", encoding="utf-8")
        with self.assertRaises(prompts.PromptError):  # 없는 파일
            prompts.load("c")
        (self.dir / "d.md").write_text("@include ../outside.md\n", encoding="utf-8")
        with self.assertRaises(prompts.PromptError):  # 지침 폴더 밖
            prompts.load("d")
        with self.assertRaises(prompts.PromptError):
            prompts.load("../main")

    def test_an_include_must_be_a_whole_line(self) -> None:
        (self.dir / "inline.md").write_text("문장 중간 @include common/rule.md 는 그대로 둔다\n", encoding="utf-8")
        self.assertEqual(prompts.load("inline").text, "문장 중간 @include common/rule.md 는 그대로 둔다")


class ShippedPromptTests(unittest.TestCase):
    """저장소에 들어 있는 실제 지침 파일: 프롬프트를 파일로 옮겨도 판 이름과 내용이 그대로여야 한다(캐시·결과 회귀 없음)."""

    def setUp(self) -> None:
        prompts.clear_cache()

    def test_moved_prompts_keep_their_version_names(self) -> None:
        self.assertEqual(prompts.load("interpret").version, "part-extract-3")  # 기존 해석 캐시가 무효가 되지 않는다
        self.assertEqual(prompts.load("reconcile").version, "reconcile-1")
        self.assertEqual(prompts.load("project-info").version, "project-info-1")
        self.assertEqual(worklog_bridge.EXTRACT_PROMPT_VERSION, prompts.load("interpret").version)

    def test_moved_prompts_say_what_they_said_before(self) -> None:
        interpret = prompts.load("interpret").text
        self.assertTrue(interpret.startswith("너는 팀 회의·멘토링 메모에서 항목을 뽑는 분석기야."))
        self.assertIn('JSON 하나만 출력해: {"items": [...]}. 뽑을 것이 없으면 {"items": []}.', interpret)
        reconcile = prompts.load("reconcile").text
        self.assertTrue(reconcile.startswith("너는 새 기록에서 나온 항목(item)을 기존 Task·Decision 목록(related)과 비교하는 판정기야."))
        self.assertIn('"superseding": 기존 결정을 명시적으로 대체함 (decision끼리만)', reconcile)
        self.assertTrue(prompts.load("project-info").text.startswith("너는 팀 소개/프로젝트 정보 페이지 원문을"))

    def test_common_guidelines_load(self) -> None:
        for name in ("common/evidence", "common/states", "common/output-json"):
            loaded = prompts.load(name)
            self.assertTrue(loaded.text.strip(), name)
        self.assertIn("검증됨", prompts.load("common/states").text)  # GitHub 완료 ≠ 검증

    def test_extraction_and_judging_send_the_file_text(self) -> None:
        sent: list[str] = []

        def fake_call(system_prompt, payload, priority=0):
            sent.append(system_prompt)
            return {"items": []}, {}, "fake:model"

        with patch.object(worklog_bridge, "_llm_call", fake_call), patch.object(worklog_bridge, "GROQ_API_KEY", "x"):
            worklog_bridge.groq_extract_part({"path": "p", "text": "t"})
            worklog_bridge.groq_judge({"title": "a"}, [])
        self.assertEqual(sent, [prompts.load("interpret").text, prompts.load("reconcile").text])

    def test_editing_the_interpret_prompt_changes_the_cache_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp)
            source = prompts.PROMPT_DIR / "interpret.md"
            (copy / "interpret.md").write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
            with patch.object(prompts, "PROMPT_DIR", copy):
                prompts.clear_cache()
                self.assertEqual(worklog_bridge.EXTRACT_PROMPT_VERSION, "part-extract-3")
                with open(copy / "interpret.md", "a", encoding="utf-8") as handle:
                    handle.write("\n- 새 규칙 한 줄\n")
                touch_later(copy / "interpret.md")
                self.assertTrue(worklog_bridge.EXTRACT_PROMPT_VERSION.startswith("part-extract-3+"))
            prompts.clear_cache()


if __name__ == "__main__":
    unittest.main()
