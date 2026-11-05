"""실연동 검증은 실제 서버 로더만 쓰고 설정·비밀을 출력하지 않아야 한다."""
from __future__ import annotations

import isolation  # noqa: F401,I001

import asyncio
import contextlib
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
import verify_phase5_connections as probe  # noqa: E402


class ConnectionProbeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.snapshot = self.root / "copy.sqlite3"
        with contextlib.closing(sqlite3.connect(self.snapshot)) as c, c:
            c.execute("CREATE TABLE source_parts (scope TEXT, origin TEXT, status TEXT, page_id TEXT)")
            c.executemany("INSERT INTO source_parts VALUES (?, ?, ?, ?)", [
                ("team:test", "notion", "active", "source-one"),
                ("team:test", "notion", "active", "source-one"),
                ("team:test", "notion", "active", "source-two"),
                ("team:test", "notion", "gone", "old"),
            ])
        (self.root / "src").mkdir()
        (self.root / "team_configs.json").write_text(json.dumps({"team:test": {"github_token": "wrong-config"}}), encoding="utf-8")
        self.config_path = self.root / "src" / "team_configs.json"
        self.config_path.write_text(json.dumps({"team:test": {"github_repo": "example/repo", "github_token": "example-github-secret",
                                     "notion_token": "example-notion-secret", "notion_page_id": "root"}}), encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_uses_server_loader_and_never_prints_credentials(self):
        client = MagicMock()
        get = AsyncMock(return_value=[])
        close = AsyncMock()
        stdout = io.StringIO()
        with patch.object(probe.main, "TEAM_CONFIG_PATH", str(self.config_path)), \
             patch.object(probe.main, "_notion_client_for", return_value=client) as factory, \
             patch.object(probe.main, "_github_get", get), patch.object(probe.main, "_close_github_http_client", close), \
             patch.object(probe.logging, "disable"), patch.object(sys, "argv", ["verify", "--snapshot", str(self.snapshot)]), \
             contextlib.redirect_stdout(stdout):
            status = probe.main_cli()
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(stdout.getvalue())["notion"]["page_statuses"], {"200": 2})
        self.assertNotIn("example-github-secret", stdout.getvalue())
        self.assertNotIn("example-notion-secret", stdout.getvalue())
        self.assertNotIn("wrong-config", stdout.getvalue())
        self.assertEqual(get.await_count, 3)
        self.assertTrue(all(call.args[2] == "example-github-secret" for call in get.await_args_list))
        self.assertEqual(factory.call_args.args[0]["notion_token"], "example-notion-secret")
        self.assertEqual(client.pages.retrieve.call_count, 3)
        close.assert_awaited_once()

    def test_missing_scope_does_not_fall_back_to_root_file(self):
        self.config_path.write_text("{}", encoding="utf-8")
        with patch.object(probe.main, "TEAM_CONFIG_PATH", str(self.config_path)):
            with self.assertRaises(ValueError):
                asyncio.run(probe.verify(self.snapshot))

    def test_errors_do_not_print_exception_contents(self):
        client = MagicMock()
        client.pages.retrieve.side_effect = RuntimeError("example-secret-in-error")
        stdout = io.StringIO()
        with patch.object(probe.main, "TEAM_CONFIG_PATH", str(self.config_path)), \
             patch.object(probe.main, "_notion_client_for", return_value=client), \
             patch.object(probe.main, "_github_get", new=AsyncMock(side_effect=RuntimeError("example-secret-in-error"))), \
             patch.object(probe.main, "_close_github_http_client", new=AsyncMock()), patch.object(probe.logging, "disable"), \
             patch.object(sys, "argv", ["verify", "--snapshot", str(self.snapshot)]), contextlib.redirect_stdout(stdout):
            status = probe.main_cli()
        self.assertEqual(status, 1)
        self.assertNotIn("example-secret-in-error", stdout.getvalue())
        self.assertEqual(json.loads(stdout.getvalue())["github"]["issues"], "RuntimeError")

    def test_multiple_scopes_require_explicit_choice(self):
        with contextlib.closing(sqlite3.connect(self.snapshot)) as c, c:
            c.execute("INSERT INTO source_parts VALUES ('team:other', 'notion', 'active', 'other')")
        with self.assertRaises(ValueError):
            probe.snapshot_sources(self.snapshot)
        self.assertEqual(probe.snapshot_sources(self.snapshot, "team:test")[0], "team:test")

    def test_rejects_real_database(self):
        target = self.root / "worklog.sqlite3"
        target.touch()
        with patch.object(probe, "SERVER", self.root / "server"), self.assertRaises(ValueError):
            probe.snapshot_sources(target)


if __name__ == "__main__":
    unittest.main()
