"""验证分块策略经过工具入口注入，重启后仍能正确识别并重建索引。"""

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from langchain_core.documents import Document

from workstation_agent.bootstrap import build_tool_application
from workstation_agent.config import Settings
from workstation_agent.rag.chunking.contracts import ChunkingSignature
from workstation_agent.rag.chunking.structured import StructuredChunkingStrategy
from workstation_agent.rag.config import RagSettings
from workstation_agent.rag.models import SourceDocument


class RecordingChunkingStrategy:
    """保留默认分块规则，使用独立版本标记验证注入与重启后的兼容检查。"""

    def __init__(self, settings: RagSettings) -> None:
        self.delegate = StructuredChunkingStrategy(settings)
        self.calls = 0

    @property
    def signature(self) -> ChunkingSignature:
        return ("fixture-recording", *self.delegate.signature)

    def split(self, document: SourceDocument, text: str) -> tuple[Document, ...]:
        self.calls += 1
        return self.delegate.split(document, text)


class ChunkingWiringTests(unittest.TestCase):
    def test_injection_restart_reindex_and_write_policy(self) -> None:
        with TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / "guide.md").write_text("# 设置\n使用中文交流。", encoding="utf-8")
            settings = Settings(workspace)
            strategy = RecordingChunkingStrategy(settings.rag)
            writer = build_tool_application(settings, approve_rag_write=True, chunking=strategy)
            self.assertTrue(writer.execute_tool("ingest_document", {"path": "guide.md"}).success)
            self.assertEqual(strategy.calls, 1)

            restarted_strategy = RecordingChunkingStrategy(settings.rag)
            reader = build_tool_application(settings, chunking=restarted_strategy)
            found = reader.execute_tool("search_documents", {"query": "中文"})
            self.assertTrue(found.success)
            self.assertEqual(json.loads(found.content)["evidence"][0]["source_uri"], "guide.md")
            self.assertEqual(restarted_strategy.calls, 0)
            denied = reader.execute_tool("ingest_document", {"path": "guide.md"})
            self.assertEqual(denied.error_code, "permission_required")

            default = build_tool_application(settings)
            mismatch = default.execute_tool("search_documents", {"query": "中文"})
            self.assertEqual(mismatch.error_code, "index_incompatible")
            rebuilder = build_tool_application(settings, approve_rag_write=True)
            rebuilt = rebuilder.execute_tool("ingest_document", {"path": "guide.md"})
            self.assertTrue(rebuilt.success)
            self.assertEqual(json.loads(rebuilt.content)["status"], "indexed")
            self.assertTrue(default.execute_tool("search_documents", {"query": "中文"}).success)
