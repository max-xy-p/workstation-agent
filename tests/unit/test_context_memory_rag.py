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
from workstation_agent.rag import (
    Document,
    InMemoryDocumentRepository,
    KeywordRetriever,
    TextChunk,
)


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
        documents = InMemoryDocumentRepository()
        document = Document(source_uri="README.md", title="README", checksum="abc")
        chunk = TextChunk(document_id=document.id, ordinal=0, content="Python setup guide")
        documents.upsert_document(document, (chunk,))

        messages, sources = ContextBuilder(
            memory=memory, retriever=KeywordRetriever(documents)
        ).build("Python setup", "Be helpful", scope_id="project_1")

        self.assertEqual(messages[0].role, "system")
        self.assertIn("Use Python", messages[1].content)
        self.assertIn("README", messages[1].content)
        self.assertEqual(sources.memory_ids, (record.id,))
        self.assertEqual(sources.chunk_ids, (chunk.id,))


if __name__ == "__main__":
    unittest.main()
