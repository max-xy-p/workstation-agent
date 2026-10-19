"""验证真实 SQLite、受控工具和中文召回的长期记忆闭环。"""

import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from workstation_agent.bootstrap import build_memory_application
from workstation_agent.config import Settings
from workstation_agent.memory.config import MemorySettings
from workstation_agent.memory.models import MemoryRecord, MemorySource
from workstation_agent.storage.sqlite.memory import SQLiteMemoryRepository
from workstation_agent.tools.contracts import ToolError


class MemoryIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.settings = Settings(self.root, memory=MemorySettings(project_id="one"))
        self.app = build_memory_application(self.settings, approve_write=True, approve_delete=True)

    def add(self, content="项目使用 Python 和 uv 管理依赖", **kwargs):
        return self.app.execute("remember_memory", {"content": content, **kwargs})

    def test_read_and_denied_write_never_create_storage(self):
        app = build_memory_application(self.settings)
        self.assertEqual(app.execute("list_memories", {})["total"], 0)
        result = app.tools.execute_tool("remember_memory", {"content": "使用中文"})
        self.assertEqual(result.error_code, "permission_required")
        self.assertFalse(self.settings.memory_database_path.exists())

    def test_restart_crud_chinese_recall_and_unrelated_exclusion(self):
        record = self.add()
        restarted = build_memory_application(self.settings, approve_write=True, approve_delete=True)
        self.assertEqual(restarted.search("怎么管理依赖")[0].id, record["id"])
        self.assertEqual(restarted.search("天气预报"), ())
        self.assertEqual(restarted.execute("get_memory", {"memory_id": record["id"]})["version"], 1)
        updated = restarted.execute(
            "update_memory",
            {
                "memory_id": record["id"],
                "expected_version": 1,
                "content": "项目改用 Poetry 管理依赖",
            },
        )
        self.assertEqual(updated["version"], 2)
        stale = restarted.tools.execute_tool(
            "update_memory",
            {
                "memory_id": record["id"],
                "expected_version": 1,
                "content": "旧值",
            },
        )
        self.assertEqual(stale.error_code, "version_conflict")
        restarted.execute("delete_memory", {"memory_id": record["id"], "expected_version": 2})
        self.assertEqual(restarted.search("管理依赖"), ())
        self.assertEqual(restarted.execute("list_memories", {})["total"], 0)

    def test_scope_applies_to_id_reads_writes_and_missing_project(self):
        record = self.add()
        for configuration in (
            MemorySettings(project_id="two"),
            MemorySettings(user_id="other", project_id="one"),
            MemorySettings(),
        ):
            other = build_memory_application(
                replace(self.settings, memory=configuration),
                approve_write=True,
                approve_delete=True,
            )
            self.assertEqual(other.search("Python"), ())
            for name, args in (
                ("get_memory", {"memory_id": record["id"]}),
                (
                    "update_memory",
                    {"memory_id": record["id"], "expected_version": 1, "content": "越权修改"},
                ),
                ("delete_memory", {"memory_id": record["id"], "expected_version": 1}),
            ):
                self.assertEqual(other.tools.execute_tool(name, args).error_code, "not_found")

    def test_expired_notes_are_manageable_but_not_recalled_and_exact_notes_deduplicate(self):
        self.add()
        self.add()
        self.assertEqual(self.app.execute("list_memories", {})["total"], 1)
        self.add("临时使用 Redis", expires_at=(datetime.now(UTC) - timedelta(days=1)).isoformat())
        self.assertEqual(self.app.search("Redis"), ())
        self.assertEqual(self.app.execute("list_memories", {})["total"], 2)

    def test_generation_invalidates_inflight_candidate_and_batch_survives_forgetting(self):
        access = self.settings.memory_access
        scope = access.scope()
        repository = SQLiteMemoryRepository(self.settings.memory_database_path, self.root)
        record = MemoryRecord(
            content="项目使用 uv",
            scope=scope,
            sources=(MemorySource(origin="extracted", run_id="batch"),),
        )
        repository.apply((record,), {record.id: None}, scope, generation=0, batch_id="batch")
        repository.delete(record.id, scope, 1)
        self.assertTrue(repository.processed(scope, "batch"))
        replacement = MemoryRecord(content="项目使用 uv", scope=scope, sources=record.sources)
        with self.assertRaises(ToolError) as caught:
            repository.apply(
                (replacement,), {replacement.id: None}, scope, generation=0, batch_id="new"
            )
        self.assertEqual(caught.exception.code, "stale_extraction")
        self.assertEqual(repository.records((scope,)), ())

    def test_sqlite_batch_rolls_back_and_capacity_is_checked_inside_transaction(self):
        scope = self.settings.memory_access.scope()
        repository = SQLiteMemoryRepository(
            self.settings.memory_database_path, self.root, max_records=1
        )
        records = tuple(
            MemoryRecord(
                content=text, scope=scope, sources=(MemorySource(origin="extracted", run_id="run"),)
            )
            for text in ("使用中文", "使用 Python")
        )
        with self.assertRaises(ToolError):
            repository.apply(records, {r.id: None for r in records}, scope)
        self.assertEqual(repository.records((scope,)), ())

    def test_secrets_and_external_data_path_are_rejected_without_echo(self):
        result = self.app.tools.execute_tool("remember_memory", {"content": "api_key=private123"})
        self.assertEqual(result.error_code, "sensitive_content")
        self.assertNotIn("private123", result.content)
        self.assertFalse(self.settings.memory_database_path.exists())
        outside_settings = replace(self.settings, data_dir=self.root.parent)
        with self.assertRaises(ValueError):
            build_memory_application(outside_settings)
