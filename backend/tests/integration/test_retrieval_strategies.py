"""验证应用组装入口能分别注入记忆和知识库策略，并继续经过工具授权。"""

import json
import unittest
from collections.abc import Sequence
from pathlib import Path
from tempfile import TemporaryDirectory

from workstation_agent.bootstrap import build_memory_application, build_tool_application
from workstation_agent.config import Settings
from workstation_agent.memory.models import MemoryRecord
from workstation_agent.rag.index import IndexSnapshot
from workstation_agent.retrieval.strategy import RetrievalHit


class SelectLastStrategy[T]:
    """选取最后一个候选，只验证注入链路，不模拟相关性算法。"""

    def __init__(self) -> None:
        self.calls = 0

    def retrieve(
        self, query: str, candidates: Sequence[T], *, limit: int
    ) -> tuple[RetrievalHit, ...]:
        self.calls += 1
        selected = candidates[-1]
        if isinstance(selected, MemoryRecord):
            return (RetrievalHit(selected.id, 1.0),)
        if isinstance(selected, IndexSnapshot):
            identifier = selected.chunks[-1].id
            assert identifier is not None
            return (RetrievalHit(identifier, 1.0),)
        raise AssertionError("候选类型不符")


class RetrievalWiringTests(unittest.TestCase):
    def test_tool_application_uses_separately_injected_strategies(self) -> None:
        with TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / "guide.txt").write_text("Python installation", encoding="utf-8")
            memory_strategy = SelectLastStrategy[MemoryRecord]()
            knowledge_strategy = SelectLastStrategy[IndexSnapshot]()
            app = build_tool_application(
                Settings(workspace),
                approve_rag_write=True,
                approve_memory_write=True,
                memory_retrieval=memory_strategy,
                knowledge_retrieval=knowledge_strategy,
            )
            self.assertTrue(app.execute_tool("ingest_document", {"path": "guide.txt"}).success)
            self.assertTrue(app.execute_tool("remember_memory", {"content": "使用中文"}).success)
            memory = app.execute_tool("search_memory", {"query": "unmatched"})
            knowledge = app.execute_tool("search_documents", {"query": "unmatched"})
            self.assertTrue(memory.success)
            self.assertTrue(knowledge.success)
            self.assertEqual(len(json.loads(memory.content)["records"]), 1)
            self.assertEqual(json.loads(knowledge.content)["status"], "ok")
            self.assertEqual((memory_strategy.calls, knowledge_strategy.calls), (1, 1))

    def test_memory_application_injection_preserves_readonly_policy(self) -> None:
        with TemporaryDirectory() as directory:
            settings = Settings(Path(directory))
            writer = build_memory_application(settings, approve_write=True)
            writer.execute("remember_memory", {"content": "使用中文"})
            strategy = SelectLastStrategy[MemoryRecord]()
            reader = build_memory_application(settings, retrieval=strategy)
            self.assertEqual(reader.search("unmatched")[0].content, "使用中文")
            denied = reader.tools.execute_tool("remember_memory", {"content": "新的偏好"})
            self.assertEqual(denied.error_code, "permission_required")
            self.assertEqual(strategy.calls, 1)
