from __future__ import annotations

import unittest

from workstation_agent.context import ContextBuilder
from workstation_agent.memory import (
    InMemoryMemoryRepository,
    MemoryKind,
    MemoryRecord,
    MemoryScope,
    MemoryService,
)
from langchain_core.runnables import RunnableLambda

from workstation_agent.application.rag import RagApplication
from workstation_agent.rag.config import RagSettings
from workstation_agent.rag.repository import InMemoryIndexRepository
from workstation_agent.rag.retriever import HybridSearch


class ContextBuilderTests(unittest.TestCase):
    def test_adds_scoped_memory_and_rag_evidence(self) -> None:
        memory = MemoryService(InMemoryMemoryRepository())
        record = memory.remember(
            MemoryRecord(
                content="Use Python for this project",
                kind=MemoryKind.SEMANTIC,
                scope=MemoryScope.PROJECT,
                scope_id="project_1",
            )
        )
        repository = InMemoryIndexRepository()
        RagApplication(repository, RagSettings()).ingest("README.md", "Python setup guide", {})
        chunks = list(repository.get("README.md").chunks)
        retriever = RunnableLambda(lambda query: HybridSearch.lexical(chunks, query, 5))
        messages, sources = ContextBuilder(memory=memory, retriever=retriever).build(
            "Python setup", "Be helpful", scope_id="project_1"
        )

        self.assertEqual(messages[0].role, "system")
        self.assertIn("Use Python", messages[1].content)
        self.assertIn("README", messages[1].content)
        self.assertEqual(sources.memory_ids, (record.id,))
        self.assertEqual(sources.chunk_ids, (chunks[0].id,))


if __name__ == "__main__":
    unittest.main()
