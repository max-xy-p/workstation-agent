import unittest
from dataclasses import replace

from langchain_core.documents import Document

from workstation_agent.application.rag import RagApplication
from workstation_agent.rag.chunking.structured import StructuredChunkingStrategy
from workstation_agent.rag.config import RagSettings
from workstation_agent.rag.index import IndexSnapshot, terms, validate_vectors
from workstation_agent.rag.models import SourceDocument
from workstation_agent.rag.repository import InMemoryIndexRepository
from workstation_agent.rag.retriever import HybridSearch
from workstation_agent.tools.contracts import ToolError


class ChunkingTests(unittest.TestCase):
    def split(self, text, size=80):
        document = SourceDocument(
            source_uri="guide.md",
            title="Guide",
            checksum="v1",
            id="doc",
            media_type="text/markdown",
        )
        return StructuredChunkingStrategy(RagSettings(chunk_size=size, chunk_overlap=8)).split(
            document, text
        )

    def test_preserves_crlf_offsets_and_all_nonblank_characters(self):
        text = "# 一级\r\n" + "中文句子。" * 30 + "\r\n## 二级\r\n" + "x" * 120
        chunks = self.split(text)
        covered = set()
        for chunk in chunks:
            self.assertEqual(
                chunk.page_content,
                text[chunk.metadata["source_start"] : chunk.metadata["source_end"]],
            )
            self.assertLessEqual(len(chunk.page_content), 80)
            self.assertEqual(
                chunk.metadata["line_start"],
                text.count("\n", 0, chunk.metadata["source_start"]) + 1,
            )
            covered.update(range(chunk.metadata["source_start"], chunk.metadata["source_end"]))
        self.assertTrue(all(i in covered for i, value in enumerate(text) if not value.isspace()))
        self.assertEqual(chunks[-1].metadata["heading_path"], "一级 → 二级")
        self.assertEqual(chunks, self.split(text))

    def test_code_heading_and_table_are_not_split_when_small(self):
        fence = chr(96) * 3
        code = fence + "python\n# not heading\nprint(1)\n" + fence + "\n"
        table = "| key | value |\n| --- | --- |\n| api | 1 |\n"
        chunks = self.split("# Title\n" + code + table, 100)
        self.assertEqual([c.metadata["heading_path"] for c in chunks], ["Title"] * len(chunks))
        self.assertTrue(any(c.page_content == code for c in chunks))
        self.assertTrue(any(c.page_content == table for c in chunks))
        self.assertTrue(any("表头" in c.metadata["auxiliary_context"] for c in chunks))

    def test_no_neighbors_cross_sections_and_long_lines_terminate(self):
        chunks = self.split("# A\n" + "a" * 220 + "\n# B\nb")
        last_a = next(c for c in reversed(chunks) if c.metadata["heading_path"] == "A")
        self.assertIsNone(last_a.metadata["next_chunk_id"])
        self.assertIsNone(chunks[-1].metadata["previous_chunk_id"])
        self.assertEqual(self.split(""), ())

    def test_long_code_and_table_keep_auxiliary_context(self):
        fence = chr(96) * 3
        text = "# A\n" + fence + "python\n" + ("print(1)\n" * 30) + fence + "\n"
        text += "| k | v |\n| --- | --- |\n" + "| row | val |\n" * 30
        chunks = self.split(text)
        self.assertGreater(sum("代码语言" in c.metadata["auxiliary_context"] for c in chunks), 1)
        self.assertGreater(sum("表头" in c.metadata["auxiliary_context"] for c in chunks), 1)
        self.assertTrue(all(len(c.page_content) <= 80 for c in chunks))

    def test_setext_heading_and_repeated_crlf_passages_keep_source_positions(self):
        text = "一级\r\n====\r\n" + ("重复中文。\r\n" * 20) + "\r\n二级\r\n----\r\n结尾"
        chunks = self.split(text)
        self.assertEqual(chunks[0].metadata["heading_path"], "一级")
        self.assertEqual(chunks[-1].metadata["heading_path"], "一级 → 二级")
        covered = set()
        for chunk in chunks:
            self.assertEqual(
                chunk.page_content,
                text[chunk.metadata["source_start"] : chunk.metadata["source_end"]],
            )
            covered.update(range(chunk.metadata["source_start"], chunk.metadata["source_end"]))
        self.assertTrue(all(i in covered for i, value in enumerate(text) if not value.isspace()))

    def test_plain_text_does_not_interpret_markdown_and_unicode_line_separator_is_preserved(self):
        text = "# literal\r\n正文\u2028第二段\r\n" + "重复" * 40
        document = SourceDocument(
            source_uri="a.txt", title="a", checksum="v", media_type="text/plain"
        )
        chunks = StructuredChunkingStrategy(RagSettings(chunk_size=32, chunk_overlap=4)).split(
            document, text
        )
        self.assertTrue(all(chunk.metadata["heading_path"] is None for chunk in chunks))
        for chunk in chunks:
            self.assertEqual(
                chunk.page_content,
                text[chunk.metadata["source_start"] : chunk.metadata["source_end"]],
            )


class RankingTests(unittest.TestCase):
    def test_chinese_and_identifiers(self):
        self.assertIn("权限", terms("工具权限 ToolExecutor ERR-403 api_key"))
        self.assertIn("err-403", terms("ERR-403"))
        self.assertIn("api_key", terms("api_key"))
        app = RagApplication(InMemoryIndexRepository(), RagSettings())
        app.ingest("api.txt", "api", {})
        app.ingest("other.txt", "other " * 5, {})
        self.assertEqual(app.search("unknown", {}, 6)["status"], "no_evidence")
        self.assertEqual(app.search("api", {}, 6)["evidence"][0]["source_uri"], "api.txt")

    def test_rrf_dedup_by_id_keeps_identical_passages_and_stable_ties(self):
        documents = [
            Document(id=f"chunk{i}", page_content="needle", metadata={"source_uri": f"{i}.txt"})
            for i in range(2)
        ]
        vectors = {doc.id: (1.0, 0.0) for doc in documents if doc.id is not None}
        results = HybridSearch().retrieve(documents, "needle", 20, vectors, [1.0, 0.0])
        self.assertEqual([doc.id for doc in results], ["chunk0", "chunk1"])
        repeated = HybridSearch().retrieve(documents, "needle", 20, vectors, [1.0, 0.0])
        self.assertEqual(results, repeated)

    def test_punctuation_only_corpus_has_no_lexical_evidence(self):
        documents = [Document(id="one", page_content="!!!")]
        self.assertEqual(HybridSearch().retrieve(documents, "needle", 20), [])

    def test_invalid_embeddings_are_rejected(self):
        for vectors in ([[0, 0]], [[float("nan")]], [[1], [1, 2]], []):
            with self.subTest(vectors=vectors), self.assertRaises(ValueError):
                validate_vectors(vectors, 1)


class FakeBackend:
    """测试夹具仅验证检索算法和模型协议，不代表真实语义效果。"""

    fingerprint = "fixture"
    fail = False

    def embed(self, texts):
        if self.fail:
            raise RuntimeError("private-test-token")
        return [[1.0, 0.0] if "权限" in t or "access" in t else [0.0, 1.0] for t in texts]

    def contextualize(self, title, background, content):
        return "介绍工具权限。"

    def rerank(self, query, texts):
        return [1.0 if "只读" in t else 0.0 for t in texts]


class RagApplicationTests(unittest.TestCase):
    def setUp(self):
        self.repository = InMemoryIndexRepository()
        self.app = RagApplication(self.repository, RagSettings(chunk_size=80, chunk_overlap=8))

    def _snapshot(self, source: str) -> IndexSnapshot:
        snapshot = self.repository.get(source)
        assert snapshot is not None
        return snapshot

    def test_ingest_idempotency_update_and_delete(self):
        first = self.app.ingest("a.md", "# 权限\n默认只读。", {"project": "A"})
        self.assertEqual(
            self.app.ingest("a.md", "# 权限\n默认只读。", {"project": "A"})["status"], "unchanged"
        )
        result = self.app.search("权限", {"project": "A"}, 6)
        self.assertEqual(result["status"], "ok")
        self.assertIn("默认只读", result["context"])
        self.assertEqual(result["used_chunk_ids"], self._ids())
        self.app.ingest("a.md", "# 权限\n允许写入。", {"project": "A"})
        self.assertNotIn("默认只读", self.app.search("权限", {}, 6)["context"])
        self.assertEqual(first["document_id"], self.repository.state().snapshots[0].source.id)
        self.assertEqual(self.app.delete("a.md")["status"], "deleted")
        self.assertEqual(self.app.search("权限", {}, 6)["status"], "no_evidence")

    def _ids(self):
        return [chunk.id for chunk in self.repository.state().snapshots[0].chunks]

    def test_filters_apply_to_expansion_and_budget_is_complete(self):
        self.app.ingest("a.md", "# 权限\n只读操作。\n\n" + "背景。" * 60, {"project": "A"})
        self.app.ingest("b.md", "# 权限\nsecret-project-B", {"project": "B"})
        result = self.app.search("只读", {"project": "A"}, 1)
        self.assertNotIn("secret-project-B", result["context"])
        self.assertTrue(all(item["source_uri"] == "a.md" for item in result["evidence"]))
        tiny = RagApplication(self.repository, replace(self.app.settings, context_budget=1))
        result = tiny.search("只读", {"project": "A"}, 1)
        self.assertEqual(result["status"], "budget_exceeded")
        self.assertEqual(result["used_chunk_ids"], [])
        self.assertEqual(result["context"], "")

    def test_hybrid_semantic_fixture_rerank_and_failed_update_keeps_old(self):
        backend = FakeBackend()
        settings = RagSettings(embedding_model="fixture", context_model="fixture")
        app = RagApplication(self.repository, settings, backend)
        app.ingest("a.md", "# 权限\n默认只读。", {})
        app.ingest("b.md", "# 食物\n苹果。", {})
        result = app.search("access", {}, 6)
        self.assertEqual(result["mode"], "hybrid")
        self.assertEqual([v["source_uri"] for v in result["evidence"]], ["a.md"])
        self.assertNotIn("介绍工具权限", result["context"])
        backend.fail = True
        with self.assertRaises(RuntimeError):
            app.ingest("a.md", "# 权限\n修改内容。", {})
        self.assertIn("默认只读", self._snapshot("a.md").chunks[0].page_content)

    def test_changed_embedding_configuration_requires_reingest(self):
        self.app.ingest("a.md", "# 权限\n只读。", {})
        app = RagApplication(self.repository, RagSettings(embedding_model="fixture"), FakeBackend())
        with self.assertRaises(ToolError) as caught:
            app.search("权限", {}, 6)
        self.assertEqual(caught.exception.code, "index_incompatible")

    def test_old_pipeline_fingerprint_requires_reingest_even_when_source_is_unchanged(self):
        text = "# 权限\n只读。"
        self.app.ingest("a.md", text, {})
        old = self._snapshot("a.md")
        self.repository.publish(old.model_copy(update={"fingerprint": "old-handwritten-pipeline"}))
        self.assertEqual(self.app.status()["status"], "needs_reindex")
        with self.assertRaises(ToolError) as caught:
            self.app.search("权限", {}, 6)
        self.assertEqual(caught.exception.code, "index_incompatible")
        self.assertEqual(self.app.ingest("a.md", text, {})["status"], "indexed")
        self.assertEqual(self.app.status()["status"], "ready")
        self.assertEqual(self.app.ingest("a.md", text, {})["status"], "unchanged")

    def test_framework_reranker_rejects_invalid_scores_without_changing_published_index(self):
        class InvalidScoreBackend(FakeBackend):
            scores = []

            def rerank(self, query, texts):
                return self.scores

        backend = InvalidScoreBackend()
        app = RagApplication(self.repository, RagSettings(context_model="fixture"), backend)
        app.ingest("a.md", "# 权限\n只读。", {})
        app.ingest("b.md", "# 权限\n审批。", {})
        for scores in ([], [0.5], [True, 1.0], [float("nan"), 0.5], [-0.1, 0.5], [1.1, 0.5]):
            with self.subTest(scores=scores), self.assertRaises(ToolError) as caught:
                backend.scores = scores
                app.search("权限", {}, 6)
            self.assertEqual(caught.exception.code, "invalid_model_output")
            self.assertEqual(app.status()["status"], "ready")

    def test_mutating_returned_framework_document_does_not_change_published_snapshot(self):
        self.app.ingest("a.md", "# 权限\n默认只读。", {})
        snapshot = self._snapshot("a.md")
        snapshot.chunks[0].page_content = "caller mutated document"
        snapshot.chunks[0].metadata["source_uri"] = "outside.md"
        result = self.app.search("权限", {}, 6)
        self.assertIn("默认只读", result["context"])
        self.assertEqual(result["evidence"][0]["source_uri"], "a.md")

    def test_snapshot_rejects_vectors_for_unknown_ids_without_replacing_published_version(self):
        self.app.ingest("a.md", "# 权限\n只读。", {})
        snapshot = self._snapshot("a.md")
        invalid = snapshot.model_copy(update={"vectors": {"unknown": (1.0, 0.0)}})
        with self.assertRaises(ValueError):
            self.repository.publish(invalid)
        self.assertEqual(self._snapshot("a.md").vectors, {})

    def test_resource_limits_do_not_publish(self):
        app = RagApplication(
            self.repository, RagSettings(chunk_size=32, chunk_overlap=0, max_chunks=1)
        )
        with self.assertRaises(ToolError):
            app.ingest("a.txt", "x" * 100, {})
        self.assertEqual(self.repository.state().snapshots, ())


class RagBudgetTests(unittest.TestCase):
    def test_model_call_budget_rejected_before_any_call(self):
        backend = FakeBackend()
        repository = InMemoryIndexRepository()
        settings = RagSettings(
            chunk_size=32, chunk_overlap=0, context_model="fixture", max_model_calls=1
        )
        app = RagApplication(repository, settings, backend)
        with self.assertRaises(ToolError) as caught:
            app.ingest("a.txt", "x" * 100, {})
        self.assertEqual(caught.exception.code, "resource_limit")
        self.assertEqual(repository.state().snapshots, ())

    def test_query_rerank_budget_is_checked_before_model_call(self):
        class RecordingBackend(FakeBackend):
            calls = 0

            def rerank(self, query, texts):
                self.calls += 1
                return super().rerank(query, texts)

        backend = RecordingBackend()
        app = RagApplication(
            InMemoryIndexRepository(),
            RagSettings(context_model="fixture", max_model_calls=1),
            backend,
        )
        app.ingest("a.md", "# 权限\n只读。", {})
        app.ingest("b.md", "# 权限\n审批。", {})
        with self.assertRaises(ToolError) as caught:
            app.search("权限", {}, 6)
        self.assertEqual(caught.exception.code, "resource_limit")
        self.assertEqual(backend.calls, 0)

    def test_parent_expansion_keeps_original_and_respects_filter(self):
        repository = InMemoryIndexRepository()
        app = RagApplication(
            repository,
            RagSettings(
                chunk_size=32,
                chunk_overlap=0,
                expansion=0,
                expand_parent=True,
            ),
        )
        text = "# Topic\nneedle " + "x" * 150 + "\nlast"
        app.ingest("a.md", text, {"project": "A"})
        app.ingest("b.md", "# Topic\nother", {"project": "B"})
        result = app.search("needle", {"project": "A"}, 1)
        self.assertEqual(result["evidence"][0]["content"], text)
        self.assertEqual([item["source_uri"] for item in result["evidence"]], ["a.md"])
