"""验证分块策略替换、索引指纹和所有实现共用的原文校验。"""

import unittest
from collections.abc import Callable
from functools import partial
from hashlib import sha256

from langchain_core.documents import Document

from workstation_agent.application.rag import RagApplication
from workstation_agent.rag.chunking.contracts import ChunkingSignature
from workstation_agent.rag.chunking.structured import StructuredChunkingStrategy
from workstation_agent.rag.config import RagSettings
from workstation_agent.rag.identifiers import stable_id
from workstation_agent.rag.index import INDEX_TEXT_VERSION
from workstation_agent.rag.models import SourceDocument
from workstation_agent.rag.repository import InMemoryIndexRepository
from workstation_agent.tools.contracts import ToolError


class WholeDocumentStrategy:
    """测试用整篇分块，仅用于足够短的文档，不代表新增生产分块算法。"""

    def __init__(self, settings: RagSettings, revision: str = "v1") -> None:
        self.settings = settings
        self.revision = revision
        self.calls = 0

    @property
    def signature(self) -> ChunkingSignature:
        return ("fixture-whole", self.revision, self.settings.chunk_size)

    def split(self, document: SourceDocument, text: str) -> tuple[Document, ...]:
        self.calls += 1
        if not text.strip():
            return ()
        return (
            Document(
                id=stable_id("chunk", document.id, document.checksum, self.revision),
                page_content=text,
                metadata={
                    "document_id": document.id,
                    "source_uri": document.source_uri,
                    "title": document.title,
                    "checksum": document.checksum,
                    "media_type": document.media_type,
                    "ordinal": 0,
                    "heading_path": None,
                    "section_id": "fixture-section",
                    "source_start": 0,
                    "source_end": len(text),
                    "line_start": 1,
                    "line_end": text.count("\n", 0, len(text) - 1) + 1,
                    "context_prefix": "",
                    "auxiliary_context": "",
                    "context_provenance": self.revision,
                    "previous_chunk_id": None,
                    "next_chunk_id": None,
                },
            ),
        )


class TransformingStrategy:
    """故意破坏默认分块结果，验证校验不依赖具体策略实现。"""

    def __init__(
        self,
        settings: RagSettings,
        transform: Callable[[tuple[Document, ...]], tuple[Document, ...]],
    ) -> None:
        self.delegate = StructuredChunkingStrategy(settings)
        self.transform = transform

    @property
    def signature(self) -> ChunkingSignature:
        return ("fixture-invalid", *self.delegate.signature)

    def split(self, document: SourceDocument, text: str) -> tuple[Document, ...]:
        return self.transform(self.delegate.split(document, text))


class RecordingBackend:
    """记录模型调用，确保无效分块在产生额外模型请求前已被拒绝。"""

    fingerprint = "fixture"

    def __init__(self) -> None:
        self.calls = 0

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        return [[1.0, 0.0] for _ in texts]

    def contextualize(self, title: str, background: str, content: str) -> str:
        self.calls += 1
        return "文档背景"

    def rerank(self, query: str, texts: list[str]) -> list[float]:
        self.calls += 1
        return [1.0 for _ in texts]


class ChunkingStrategyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = InMemoryIndexRepository()
        self.settings = RagSettings(chunk_size=80, chunk_overlap=8)
        self.text = "前言\r\n# A\r\n第一节正文。\r\n# B\r\n第二节正文。"

    def test_injected_strategy_changes_boundaries_and_preserves_citations(self) -> None:
        default = RagApplication(self.repository, self.settings)
        default.ingest("guide.md", self.text, {})
        before = self.repository.get("guide.md")
        assert before is not None
        self.assertGreater(len(before.chunks), 1)
        strategy = WholeDocumentStrategy(self.settings)
        app = RagApplication(self.repository, self.settings, chunking=strategy)
        self.assertEqual(app.ingest("guide.md", self.text, {})["chunks"], 1)
        after = self.repository.get("guide.md")
        assert after is not None
        self.assertEqual(after.source.id, before.source.id)
        self.assertEqual(after.chunks[0].page_content, self.text)
        result = app.search("正文", {}, 1)
        self.assertEqual(result["evidence"][0]["content"], self.text)
        self.assertEqual(result["evidence"][0]["line_start"], 1)
        self.assertEqual(result["evidence"][0]["line_end"], 5)
        self.assertEqual(strategy.calls, 1)

    def test_strategy_revision_invalidates_cache_and_requires_reingest(self) -> None:
        first_strategy = WholeDocumentStrategy(self.settings)
        first = RagApplication(self.repository, self.settings, chunking=first_strategy)
        first.ingest("guide.md", self.text, {})
        second_strategy = WholeDocumentStrategy(self.settings, "v2")
        second = RagApplication(self.repository, self.settings, chunking=second_strategy)
        self.assertNotEqual(first.fingerprint, second.fingerprint)
        self.assertEqual(second.status()["status"], "needs_reindex")
        with self.assertRaises(ToolError) as caught:
            second.search("正文", {}, 1)
        self.assertEqual(caught.exception.code, "index_incompatible")
        self.assertEqual(second.ingest("guide.md", self.text, {})["status"], "indexed")
        self.assertEqual(second.ingest("guide.md", self.text, {})["status"], "unchanged")
        self.assertEqual(second.status()["status"], "ready")
        self.assertEqual(second_strategy.calls, 1)

    def test_default_pipeline_fingerprint_matches_existing_index(self) -> None:
        parts = ("native-document-v3", 80, 8, "terms-v1", INDEX_TEXT_VERSION, "lexical")
        expected = sha256(repr(parts).encode()).hexdigest()
        app = RagApplication(self.repository, self.settings)
        self.assertEqual(app.fingerprint, expected)
        app.ingest("guide.md", self.text, {})
        previous = self.repository.get("guide.md")
        assert previous is not None
        self.assertEqual(previous.fingerprint, expected)
        for chunk in previous.chunks:
            self.assertEqual(
                chunk.id,
                stable_id(
                    "chunk",
                    previous.source.id,
                    previous.source.checksum,
                    "native-document-v3",
                    "80",
                    "8",
                    str(chunk.metadata["source_start"]),
                    str(chunk.metadata["source_end"]),
                ),
            )
        injected = RagApplication(
            self.repository, self.settings, chunking=StructuredChunkingStrategy(self.settings)
        )
        self.assertEqual(injected.ingest("guide.md", self.text, {})["status"], "unchanged")

    def test_invalid_results_never_call_models_or_replace_published_index(self) -> None:
        def edit(name: str, chunks: tuple[Document, ...]) -> tuple[Document, ...]:
            first = chunks[0]
            metadata = dict(first.metadata)
            if name == "missing_fields":
                metadata.pop("source_uri")
            elif name == "foreign_source":
                metadata["source_uri"] = "another.md"
            elif name == "offset":
                metadata["source_start"] += 1
            elif name == "line_number":
                metadata["line_start"] = 99
            elif name == "neighbor":
                metadata["next_chunk_id"] = "foreign-id"
            elif name == "ordinal":
                metadata["ordinal"] = True
            elif name == "changed_content":
                return (first.model_copy(update={"page_content": "伪造正文"}), *chunks[1:])
            elif name == "duplicate_id":
                return (first, first)
            elif name == "omitted_tail":
                return (first.model_copy(update={"metadata": {**metadata, "next_chunk_id": None}}),)
            elif name == "empty":
                return ()
            return (first.model_copy(update={"metadata": metadata}), *chunks[1:])

        backend = RecordingBackend()
        settings = RagSettings(
            chunk_size=80, chunk_overlap=8, embedding_model="fixture", context_model="fixture"
        )
        original = RagApplication(self.repository, settings, backend)
        original.ingest("guide.md", self.text, {})
        previous = self.repository.get("guide.md")
        for name in (
            "missing_fields",
            "foreign_source",
            "offset",
            "line_number",
            "neighbor",
            "ordinal",
            "changed_content",
            "duplicate_id",
            "omitted_tail",
            "empty",
        ):
            backend.calls = 0
            strategy = TransformingStrategy(settings, partial(edit, name))
            app = RagApplication(self.repository, settings, backend, chunking=strategy)
            with self.subTest(name=name), self.assertRaises(ToolError) as caught:
                app.ingest("guide.md", self.text, {})
            self.assertEqual(caught.exception.code, "invalid_chunking_output")
            self.assertEqual(backend.calls, 0)
            self.assertEqual(self.repository.get("guide.md"), previous)

    def test_common_count_and_length_limits_apply_to_custom_strategy(self) -> None:
        cases = (
            (RagSettings(chunk_size=32, chunk_overlap=0), "正文" * 30, "whole"),
            (RagSettings(chunk_size=80, chunk_overlap=8, max_chunks=1), "正文", "many"),
        )
        for settings, text, mode in cases:
            strategy = WholeDocumentStrategy(settings)
            if mode == "many":
                invalid = TransformingStrategy(settings, lambda chunks: chunks * 2)
                app = RagApplication(self.repository, settings, chunking=invalid)
            else:
                app = RagApplication(self.repository, settings, chunking=strategy)
            with self.subTest(mode=mode), self.assertRaises(ToolError) as caught:
                app.ingest("guide.md", text, {})
            self.assertEqual(caught.exception.code, "resource_limit")
            self.assertEqual(self.repository.state().snapshots, ())

    def test_whitespace_only_input_can_have_no_chunks(self) -> None:
        app = RagApplication(
            self.repository, self.settings, chunking=WholeDocumentStrategy(self.settings)
        )
        self.assertEqual(app.ingest("empty.txt", " \r\n ", {})["chunks"], 0)
