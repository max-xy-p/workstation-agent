"""验证 RAG 文件、PostgreSQL、权限及真实 CLI 边界。"""

import json
import os
import subprocess
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from tests.postgres_support import has_rag
from tests.unit.test_rag import FakeBackend
from workstation_agent.bootstrap import build_tool_application, database_for
from workstation_agent.config import Settings
from workstation_agent.rag.config import RagSettings


class RagIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.settings = Settings(self.root)
        (self.root / "guide.md").write_bytes("# 权限\r\nToolExecutor 默认只读。\r\n".encode())

    def application(self, write=False, settings=None, backend=None):
        return build_tool_application(
            settings or self.settings, approve_rag_write=write, rag_backend=backend
        )

    def call(self, app, name, args):
        result = app.execute_tool(name, args)
        self.assertTrue(result.success, result)
        return json.loads(result.content)

    def test_permissions_and_read_only_missing_index_have_no_writes(self):
        app = self.application()
        denied = app.execute_tool("ingest_document", {"path": "guide.md"})
        self.assertEqual(denied.error_code, "permission_required")
        self.assertEqual(
            self.call(app, "search_documents", {"query": "权限"})["status"], "no_evidence"
        )
        self.assertFalse(has_rag(self.settings))
        denied = app.execute_tool("delete_document", {"path": "guide.md"})
        self.assertEqual(denied.error_code, "permission_required")

    def test_postgres_restart_update_delete_and_original_citations(self):
        self.call(
            self.application(True),
            "ingest_document",
            {"path": "guide.md", "metadata": {"project": "A"}},
        )
        app = self.application()
        result = self.call(
            app, "search_documents", {"query": "ToolExecutor", "filters": {"project": "A"}}
        )
        self.assertEqual(result["status"], "ok")
        raw = (self.root / "guide.md").read_bytes().decode("utf-8")
        for item in result["evidence"]:
            self.assertEqual(item["content"], raw[item["source_start"] : item["source_end"]])
            self.assertIn("guide.md:", item["citation"])
        result = self.call(
            self.application(True),
            "ingest_document",
            {"path": "guide.md", "metadata": {"project": "A"}},
        )
        self.assertEqual(result["status"], "unchanged")
        (self.root / "guide.md").write_text("# 权限\n新增写入模式", encoding="utf-8")
        self.call(self.application(True), "ingest_document", {"path": "guide.md"})
        self.assertNotIn(
            "默认只读", self.call(app, "search_documents", {"query": "权限"})["context"]
        )
        self.call(self.application(True), "delete_document", {"path": "guide.md"})
        self.assertTrue((self.root / "guide.md").exists())
        self.assertEqual(
            self.call(app, "search_documents", {"query": "权限"})["status"], "no_evidence"
        )

    def test_rejects_paths_formats_utf8_and_oversized_input(self):
        app = self.application(True)
        (self.root / "bad.txt").write_bytes(b"\xff")
        (self.root / "nul.txt").write_bytes(b"binary\0")
        for path, code in (
            ("../outside.md", "path_outside_workspace"),
            ("file.pdf", "unsupported_format"),
            ("missing.txt", "not_found"),
            ("bad.txt", "invalid_utf8"),
            ("nul.txt", "unsupported_format"),
        ):
            with self.subTest(path=path):
                result = app.execute_tool("ingest_document", {"path": path})
                self.assertEqual(result.error_code, code)
        limited = self.application(True, replace(self.settings, rag=RagSettings(max_file_bytes=1)))
        self.assertEqual(
            limited.execute_tool("ingest_document", {"path": "guide.md"}).error_code,
            "resource_limit",
        )
        self.assertFalse(has_rag(self.settings))

    def test_model_outbound_denial_and_failed_update_do_not_replace_old_version(self):
        rag = RagSettings(embedding_model="fixture")
        configured = replace(self.settings, rag=rag)
        result = self.application(True, configured, FakeBackend()).execute_tool(
            "ingest_document", {"path": "guide.md"}
        )
        self.assertEqual(result.error_code, "permission_required")
        self.assertFalse(has_rag(self.settings))
        configured = replace(configured, rag=replace(rag, allow_remote=True))
        backend = FakeBackend()
        self.call(
            self.application(True, configured, backend), "ingest_document", {"path": "guide.md"}
        )
        (self.root / "guide.md").write_text("# 权限\nnew", encoding="utf-8")
        backend.fail = True
        failed = self.application(True, configured, backend).execute_tool(
            "ingest_document", {"path": "guide.md"}
        )
        self.assertFalse(failed.success)
        self.assertNotIn("private-test-token", failed.content)
        backend.fail = False
        result = self.call(
            self.application(False, configured, backend), "search_documents", {"query": "权限"}
        )
        self.assertIn("默认只读", result["context"])

    def test_framework_vector_snapshot_reuses_embeddings_after_restart_and_filters_first(self):
        class RecordingBackend(FakeBackend):
            def __init__(self):
                self.inputs = []

            def embed(self, texts):
                self.inputs.append(list(texts))
                return super().embed(texts)

        backend = RecordingBackend()
        configured = replace(
            self.settings,
            rag=RagSettings(embedding_model="fixture", allow_remote=True),
        )
        (self.root / "other.md").write_text("# 食物\n另一项目", encoding="utf-8")
        app = self.application(True, configured, backend)
        self.call(app, "ingest_document", {"path": "guide.md", "metadata": {"project": "A"}})
        self.call(app, "ingest_document", {"path": "other.md", "metadata": {"project": "B"}})
        backend.inputs.clear()
        restarted = self.application(False, configured, backend)
        result = self.call(
            restarted, "search_documents", {"query": "access", "filters": {"project": "A"}}
        )
        self.assertEqual(backend.inputs, [["access"]])
        self.assertEqual([item["source_uri"] for item in result["evidence"]], ["guide.md"])
        backend.inputs.clear()
        result = self.call(
            restarted, "search_documents", {"query": "access", "filters": {"project": "missing"}}
        )
        self.assertEqual(result["status"], "no_evidence")
        self.assertEqual(backend.inputs, [])

    def test_invalid_queries_are_rejected_before_side_effects(self):
        app = self.application()
        for args in (
            {"query": " "},
            {"query": "x", "limit": 0},
            {"query": "x", "filters": {"project": 1}},
        ):
            self.assertEqual(
                app.execute_tool("search_documents", args).error_code, "invalid_arguments"
            )
        self.assertFalse(has_rag(self.settings))

    def test_real_cli_ingest_search_and_write_approval(self):
        assert self.settings.database_url is not None
        environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("WORKSTATION_AGENT_")
        }
        environment.update(
            WORKSTATION_AGENT_WORKSPACE=str(self.root), PYTHONIOENCODING="utf-8",
            WORKSTATION_AGENT_DATABASE_URL=self.settings.database_url,
        )

        def cli(*args):
            return subprocess.run(
                [sys.executable, "-m", "workstation_agent", "rag", *args],
                env=environment,
                text=True,
                encoding="utf-8",
                capture_output=True,
                timeout=30,
            )

        self.assertEqual(cli("ingest", "guide.md").returncode, 3)
        process = cli("ingest", "guide.md", "--approve-write")
        self.assertEqual(process.returncode, 0, process.stderr)
        process = cli("search", "ToolExecutor")
        self.assertEqual(process.returncode, 0, process.stderr)
        result = json.loads(json.loads(process.stdout)["content"])
        self.assertIn("默认只读", result["context"])
        self.assertEqual(cli("delete", "guide.md", "--approve-write").returncode, 0)

    def test_failed_postgres_publish_rolls_back_old_document(self) -> None:
        from psycopg import sql

        self.call(self.application(True), "ingest_document", {"path": "guide.md"})
        database = database_for(self.settings)
        constraint = "reject_update_" + database.workspace_id[:16]
        with database.connect() as connection:
            row = connection.execute(
                "SELECT bundle::text FROM workstation.rag_documents WHERE workspace_id=%s AND source_uri=%s",
                (database.workspace_id, "guide.md"),
            ).fetchone()
            assert row is not None
            payload = row[0]
            connection.execute(sql.SQL(
                "ALTER TABLE workstation.rag_documents ADD CONSTRAINT {} CHECK (workspace_id <> {} OR bundle = {}::jsonb)"
            ).format(sql.Identifier(constraint), sql.Literal(database.workspace_id), sql.Literal(payload)))
        try:
            (self.root / "guide.md").write_text("# 权限\n更改内容", encoding="utf-8")
            failed = self.application(True).execute_tool("ingest_document", {"path": "guide.md"})
            self.assertFalse(failed.success)
            self.assertNotIn(constraint, failed.content)
            self.assertIn("默认只读", self.call(self.application(), "search_documents", {"query": "权限"})["context"])
        finally:
            with database.connect() as connection:
                connection.execute(sql.SQL("ALTER TABLE workstation.rag_documents DROP CONSTRAINT {}").format(sql.Identifier(constraint)))

    def test_index_health_and_reserved_source_filter(self):
        self.assertEqual(self.call(self.application(), "rag_status", {})["status"], "missing")
        self.call(self.application(True), "ingest_document", {"path": "guide.md"})
        self.assertEqual(self.call(self.application(), "rag_status", {})["status"], "ready")
        result = self.call(
            self.application(),
            "search_documents",
            {
                "query": "权限",
                "filters": {"source_uri": "missing.md"},
            },
        )
        self.assertEqual(result["status"], "no_evidence")
        changed = replace(self.settings, rag=RagSettings(chunk_size=600))
        self.assertEqual(
            self.call(self.application(settings=changed), "rag_status", {})["status"],
            "needs_reindex",
        )

    def test_old_format_is_not_decoded_and_each_source_must_be_rebuilt_or_deleted(self) -> None:
        database = database_for(self.settings)
        database.initialize()
        with database.connect() as connection:
            for source in ("guide.md", "old-other.md"):
                connection.execute(
                    "INSERT INTO workstation.rag_documents VALUES (%s,%s,%s,%s::jsonb,%s)",
                    (database.workspace_id, source, "old-" + source, '{"legacy":true}', 2),
                )
        readonly = self.application()
        report = self.call(readonly, "rag_status", {})
        self.assertEqual(report["status"], "needs_reindex")
        self.assertEqual(report["obsolete_sources"], ["guide.md", "old-other.md"])
        self.assertEqual(readonly.execute_tool("search_documents", {"query": "权限"}).error_code, "index_incompatible")
        self.assertEqual(readonly.execute_tool("ingest_document", {"path": "guide.md"}).error_code, "permission_required")
        with database.connect() as connection:
            versions = connection.execute(
                "SELECT DISTINCT format_version FROM workstation.rag_documents WHERE workspace_id=%s",
                (database.workspace_id,),
            ).fetchall()
        self.assertEqual(versions, [(2,)])
        writable = self.application(True)
        self.call(writable, "ingest_document", {"path": "guide.md"})
        self.assertEqual(self.call(readonly, "rag_status", {})["obsolete_sources"], ["old-other.md"])
        result = self.call(readonly, "search_documents", {"query": "权限", "filters": {"source_uri": "guide.md"}})
        self.assertIn("默认只读", result["context"])
        self.assertEqual(readonly.execute_tool("search_documents", {"query": "权限"}).error_code, "index_incompatible")
        with database.connect() as connection:
            row = connection.execute(
                "SELECT bundle::text,format_version FROM workstation.rag_documents WHERE workspace_id=%s AND source_uri='guide.md'",
                (database.workspace_id,),
            ).fetchone()
            assert row is not None
            payload, version = row
            migration = connection.execute("SELECT COUNT(*) FROM workstation.migrations WHERE version=5").fetchone()
            assert migration is not None
            self.assertEqual(migration[0], 1)
        self.assertEqual(version, 3)
        native = json.loads(payload)
        self.assertNotIn("counts", native)
        self.assertIn("page_content", native["chunks"][0])
        self.call(writable, "delete_document", {"path": "old-other.md"})
        self.assertEqual(self.call(readonly, "rag_status", {})["status"], "ready")

    def test_concurrent_publications_respect_global_chunk_budget(self):
        from concurrent.futures import ThreadPoolExecutor

        from workstation_agent.application.rag import RagApplication
        from workstation_agent.rag.repository import InMemoryIndexRepository
        from workstation_agent.storage.postgres.rag import PostgresIndexRepository

        memory = InMemoryIndexRepository()
        app = RagApplication(memory, RagSettings())
        for name in ("one.md", "two.md"):
            app.ingest(name, "# Heading\noriginal evidence", {})
        repository = PostgresIndexRepository(
            database_for(self.settings),
            max_chunks=1,
        )

        def publish(bundle):
            try:
                repository.publish(bundle)
                return True
            except Exception:
                return False

        with ThreadPoolExecutor(max_workers=2) as pool:
            successes = list(pool.map(publish, memory.state().snapshots))
        self.assertEqual(sum(successes), 1)
        self.assertEqual(sum(len(item.chunks) for item in repository.state().snapshots), 1)
