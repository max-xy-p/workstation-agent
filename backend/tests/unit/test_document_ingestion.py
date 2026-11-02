"""验证完整外部正文经过同一 RAG 管线处理，更新失败不会覆盖已发布索引。"""

import unittest
from unittest.mock import patch

from pydantic import ValidationError

from workstation_agent.api.documents import IngestDocument
from workstation_agent.application.documents import DocumentApplication
from workstation_agent.application.rag import RagApplication
from workstation_agent.application.tools import ToolApplication
from workstation_agent.bootstrap import RagApprovalPolicy
from workstation_agent.rag.config import RagSettings
from workstation_agent.rag.repository import InMemoryIndexRepository
from workstation_agent.tools.builtin.rag import IngestExternalDocumentTool
from workstation_agent.tools.contracts import ToolError
from workstation_agent.tools.executor import ToolExecutor
from workstation_agent.tools.registry import ToolRegistry


class DocumentIngestionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = InMemoryIndexRepository()
        self.rag = RagApplication(self.repository, RagSettings())
        self.document = IngestDocument(source_uri="workstation://main/workspaces/1/notes/42", title="数据库约定",
                                       text="# PostgreSQL\n项目使用 PostgreSQL 保存数据。", media_type="text/markdown", source_version="7")

    def application(self, *, approved: bool) -> DocumentApplication:
        return DocumentApplication(ToolApplication(ToolExecutor(
            ToolRegistry((IngestExternalDocumentTool(self.rag),)), RagApprovalPolicy(approve_write=approved),
        )))

    def test_explicit_format_version_and_stable_source_are_preserved(self) -> None:
        app = self.application(approved=True)
        first = app.ingest(self.document)
        self.assertEqual(first["status"], "indexed")
        saved = self.repository.get(self.document.source_uri)
        assert saved is not None
        self.assertEqual(saved.source.title, "数据库约定")
        self.assertEqual(saved.source.media_type, "text/markdown")
        self.assertEqual(saved.source.metadata["source_version"], "7")
        self.assertTrue(any(chunk.metadata["heading_path"] == "PostgreSQL" for chunk in saved.chunks))
        self.assertEqual(app.ingest(self.document)["status"], "unchanged")
        changed = self.document.model_copy(update={"text": "# 新版\n版本更新后的正文。", "source_version": "8", "title": "新标题"})
        self.assertEqual(app.ingest(changed)["document_id"], first["document_id"])
        self.assertEqual(len(self.repository.state().snapshots), 1)

    def test_permission_and_failed_publication_leave_existing_document_intact(self) -> None:
        with self.assertRaises(ToolError) as caught:
            self.application(approved=False).ingest(self.document)
        self.assertEqual(caught.exception.code, "permission_required")
        self.assertEqual(self.repository.state().snapshots, ())
        app = self.application(approved=True)
        app.ingest(self.document)
        original = self.repository.get(self.document.source_uri)
        with patch.object(self.repository, "publish", side_effect=RuntimeError("private-storage-error")):
            with self.assertRaises(ToolError) as failed:
                app.ingest(self.document.model_copy(update={"text": "更新正文"}))
        self.assertNotIn("private-storage-error", str(failed.exception))
        self.assertEqual(self.repository.get(self.document.source_uri), original)

    def test_invalid_source_format_and_metadata_are_rejected(self) -> None:
        for updates in ({"source_uri": "../local.md"}, {"source_uri": "file:///tmp/note.md"},
                        {"source_uri": "https://example.com/doc?token=secret"}, {"media_type": "application/pdf"},
                        {"metadata": {"source_version": "spoofed"}}):
            with self.subTest(updates=updates), self.assertRaises(ValidationError):
                IngestDocument.model_validate({**self.document.model_dump(), **updates})

    def test_text_size_and_binary_content_are_checked_before_processing(self) -> None:
        for text in ("x" * (self.rag.settings.max_file_bytes + 1), "正文\0二进制"):
            with self.subTest(length=len(text)), self.assertRaises(ToolError):
                self.application(approved=True).ingest(self.document.model_copy(update={"text": text}))
        self.assertEqual(self.repository.state().snapshots, ())
