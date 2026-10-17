"""验证独立记忆和 RAG 通过唯一 Context 入口共同组装。"""

import unittest
from types import SimpleNamespace

from langchain_core.runnables import RunnableLambda

from workstation_agent.application.rag import RagApplication
from workstation_agent.context import ContextBuilder
from workstation_agent.memory import MemoryAccess, MemoryRecord, MemorySource
from workstation_agent.rag.config import RagSettings
from workstation_agent.rag.repository import InMemoryIndexRepository
from workstation_agent.rag.retriever import HybridSearch


class ContextBuilderTests(unittest.TestCase):
    def test_adds_scoped_memory_and_rag_evidence(self):
        record = MemoryRecord(
            content="Use Python for this project",
            scope=MemoryAccess(user_id="local", workspace_id="ws", project_id="one").scope(),
            sources=(MemorySource(origin="explicit", run_id="run"),),
        )
        memory = SimpleNamespace(search=lambda _: (record,))
        repository = InMemoryIndexRepository()
        RagApplication(repository, RagSettings()).ingest("README.md", "Python setup guide", {})
        chunks = list(repository.get("README.md").chunks)
        retriever = RunnableLambda(lambda query: HybridSearch.lexical(chunks, query, 5))
        items = ContextBuilder(memory=memory, retriever=retriever).retrieve("Python setup")
        self.assertIn("Use Python", items[0].content)
        self.assertIn("README", items[1].content)
        self.assertEqual(items[0].id, record.id)
        self.assertEqual(items[1].id, chunks[0].id)

    def test_memory_budget_drops_whole_note(self):
        scope = MemoryAccess(user_id="local", workspace_id="ws").scope()
        note = MemoryRecord(
            content="Python " * 300,
            scope=scope,
            sources=(MemorySource(origin="explicit", run_id="run"),),
        )
        items = ContextBuilder(
            memory=SimpleNamespace(search=lambda _: (note,)),
            max_memory_chars=100,
        ).retrieve("Python")
        self.assertEqual(items, [])
