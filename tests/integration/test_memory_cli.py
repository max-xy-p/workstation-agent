"""通过实际 CLI 入口验收本机记忆管理，无外部模型请求。"""

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


class MemoryCLIIntegrationTests(unittest.TestCase):
    def test_authorized_crud_survives_independent_processes(self):
        with TemporaryDirectory() as directory:
            env = {
                **os.environ,
                "WORKSTATION_AGENT_WORKSPACE": directory,
                "WORKSTATION_AGENT_PROJECT_ID": "one",
                "WORKSTATION_AGENT_USER_ID": "local",
                "WORKSTATION_AGENT_MEMORY_ENABLED": "true",
                "WORKSTATION_AGENT_MEMORY_AUTO_EXTRACT": "false",
                "PYTHONUTF8": "1",
            }

            def cli(*args):
                return subprocess.run(
                    [sys.executable, "-m", "workstation_agent", *args],
                    env=env,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    timeout=30,
                )

            denied = cli("memory", "add", "项目使用 Python")
            self.assertEqual(denied.returncode, 3, denied.stderr)
            self.assertFalse((Path(directory) / ".workstation-agent").exists())
            added = cli("memory", "add", "项目使用 Python", "--approve-write")
            self.assertEqual(added.returncode, 0, added.stderr)
            note = json.loads(json.loads(added.stdout)["content"])
            recalled = cli("memory", "search", "Python")
            self.assertEqual(recalled.returncode, 0, recalled.stderr)
            self.assertIn(note["id"], recalled.stdout)
            updated = cli(
                "memory", "update", note["id"], "项目使用 Go", "--version", "1", "--approve-write"
            )
            self.assertEqual(updated.returncode, 0, updated.stderr)
            stale = cli("memory", "delete", note["id"], "--version", "1", "--approve-delete")
            self.assertNotEqual(stale.returncode, 0)
            deleted = cli("memory", "delete", note["id"], "--version", "2", "--approve-delete")
            self.assertEqual(deleted.returncode, 0, deleted.stderr)
            self.assertEqual(
                json.loads(json.loads(cli("memory", "list").stdout)["content"])["total"], 0
            )
