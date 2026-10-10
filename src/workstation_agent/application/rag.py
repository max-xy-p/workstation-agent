"""编排框架文档、索引快照与证据；副作用由工具执行边界授权。"""

from hashlib import sha256

from ..context.evidence import assemble_evidence
from ..rag.backend import RagBackend, token_upper_bound
from ..rag.chunking import ChunkBudgetExceeded, DocumentChunker, stable_id
from ..rag.config import RagSettings
from ..rag.index import IndexSnapshot, index_text, validate_snapshot, validate_vectors
from ..rag.models import SourceDocument
from ..rag.repository import IndexRepository
from ..rag.retriever import FrameworkReranker, HybridSearch
from ..tools.contracts import ToolError


class RagApplication:
    """构建完整快照后原子发布，处理链统一使用框架 Document。"""

    def __init__(
        self, repository: IndexRepository, settings: RagSettings, backend: RagBackend | None = None
    ):
        self.repository = repository
        self.settings = settings
        self.backend = backend
        self.chunker = DocumentChunker(settings)

    @property
    def fingerprint(self):
        """配置和管线版本变化时要求重新摄取，不转换旧处理结果。"""
        parts = (
            self.chunker.version,
            self.settings.chunk_size,
            self.settings.chunk_overlap,
            "terms-v1",
            HybridSearch.version,
            self.backend.fingerprint if self.backend else "lexical",
        )
        return sha256(repr(parts).encode()).hexdigest()

    def ingest(self, source: str, text: str, metadata: dict[str, str]):
        """处理已授权读取的文本；发布失败时保留已有版本。"""
        checksum = sha256(text.encode("utf-8")).hexdigest()
        previous = self.repository.get(source)
        if (
            previous
            and previous.source.checksum == checksum
            and previous.fingerprint == self.fingerprint
            and previous.source.metadata == metadata
        ):
            return {
                "status": "unchanged",
                "document_id": previous.source.id,
                "chunks": len(previous.chunks),
            }
        document = SourceDocument(
            id=stable_id("doc", source),
            source_uri=source,
            title=source,
            checksum=checksum,
            media_type="text/markdown" if source.lower().endswith(".md") else "text/plain",
            metadata=metadata,
        )
        try:
            chunks = self.chunker.split(document, text)
        except ChunkBudgetExceeded:
            raise ToolError("RAG chunk budget exceeded", code="resource_limit") from None
        estimated_calls = (len(chunks) if self.settings.context_model else 0) + (
            (len(chunks) + 15) // 16 if self.settings.embedding_model else 0
        )
        if estimated_calls > self.settings.max_model_calls:
            raise ToolError("RAG model call budget exceeded", code="resource_limit")
        others = sum(
            len(item.chunks)
            for item in self.repository.state().snapshots
            if item.source.source_uri != source
        )
        if len(chunks) + others > self.settings.max_chunks:
            raise ToolError("RAG chunk budget exceeded", code="resource_limit")
        if self.settings.context_model and self.backend:
            enriched = []
            for chunk in chunks:
                section = [
                    c for c in chunks if c.metadata["section_id"] == chunk.metadata["section_id"]
                ]
                background = text[
                    section[0].metadata["source_start"] : section[-1].metadata["source_end"]
                ]
                prefix = self.backend.contextualize(document.title, background, chunk.page_content)
                if not isinstance(prefix, str) or not prefix.strip() or len(prefix) > 512:
                    raise ToolError("invalid contextual prefix", code="invalid_model_output")
                enriched.append(
                    chunk.model_copy(
                        update={
                            "metadata": {
                                **chunk.metadata,
                                "context_prefix": chunk.metadata["context_prefix"] + "\n" + prefix,
                                "context_provenance": self.backend.fingerprint,
                            }
                        }
                    )
                )
            chunks = tuple(enriched)
        texts = [index_text(chunk) for chunk in chunks]
        if any(token_upper_bound(value) > self.settings.input_budget for value in texts):
            raise ToolError("RAG index text exceeds input budget", code="resource_limit")
        vectors = []
        if self.settings.embedding_model and self.backend:
            for i in range(0, len(texts), 16):
                vectors.extend(self.backend.embed(texts[i : i + 16]))
            validate_vectors(vectors, len(chunks))
        snapshot = IndexSnapshot(
            source=document,
            chunks=chunks,
            vectors={chunk.id: tuple(vector) for chunk, vector in zip(chunks, vectors, strict=True)}
            if vectors
            else {},
            fingerprint=self.fingerprint,
        )
        self.repository.publish(snapshot)
        return {
            "status": "indexed",
            "document_id": document.id,
            "checksum": checksum,
            "chunks": len(chunks),
            "mode": "hybrid" if vectors else "bm25",
        }

    def status(self):
        """只读检查当前格式，不解码旧对象或调用模型。"""
        state = self.repository.state()
        incompatible = len(state.obsolete_sources) + sum(
            item.fingerprint != self.fingerprint for item in state.snapshots
        )
        invalid = 0
        for item in state.snapshots:
            try:
                validate_snapshot(item)
                if self.settings.embedding_model:
                    validate_vectors(list(item.vectors.values()), len(item.chunks))
            except (ValueError, TypeError, KeyError):
                invalid += 1
        documents = len(state.snapshots) + len(state.obsolete_sources)
        return {
            "status": "missing"
            if not documents
            else "needs_reindex"
            if incompatible or invalid
            else "ready",
            "documents": documents,
            "chunks": sum(len(item.chunks) for item in state.snapshots),
            "incompatible_documents": incompatible,
            "invalid_documents": invalid,
            "obsolete_sources": list(state.obsolete_sources),
            "mode": "hybrid" if self.settings.embedding_model else "bm25",
        }

    def delete(self, source: str):
        """删除指定来源的索引快照，保留原文件。"""
        return {"status": "deleted" if self.repository.delete(source) else "not_found"}

    def search(self, query: str, filters: dict[str, str], limit: int):
        """检索返回框架文档，通过块 ID 回到原文，再进行预算内扩展。"""
        if not query.strip():
            raise ToolError("query must not be blank", code="invalid_arguments")
        state = self.repository.state()
        obsolete = [
            source
            for source in state.obsolete_sources
            if "source_uri" not in filters or filters["source_uri"] == source
        ]
        snapshots = [
            item
            for item in state.snapshots
            if all(
                {
                    **item.source.metadata,
                    "source_uri": item.source.source_uri,
                    "document_id": item.source.id,
                    "media_type": item.source.media_type,
                }.get(key)
                == value
                for key, value in filters.items()
            )
        ]
        if obsolete or any(item.fingerprint != self.fingerprint for item in snapshots):
            raise ToolError(
                "RAG index format or configuration changed; re-ingest matching documents",
                code="index_incompatible",
            )
        chunks = [chunk for item in snapshots for chunk in item.chunks]
        if len(chunks) > self.settings.max_chunks:
            raise ToolError("RAG chunk budget exceeded", code="resource_limit")
        if not chunks:
            return {
                "status": "no_evidence",
                "mode": "hybrid" if self.settings.embedding_model else "bm25",
                "evidence": [],
                "context": "",
                "used_chunk_ids": [],
                "warnings": [],
            }
        query_vector = None
        if self.settings.embedding_model and self.backend:
            embeddings = self.backend.embed([query])
            validate_vectors(embeddings, 1)
            query_vector = embeddings[0]
        compressor = (
            FrameworkReranker(
                backend=self.backend,
                max_model_calls=self.settings.max_model_calls,
                prior_calls=int(bool(self.settings.embedding_model)),
            )
            if self.settings.context_model and self.backend
            else None
        )
        hits = HybridSearch().retrieve(
            chunks,
            query,
            self.settings.recall_limit,
            vectors={key: value for item in snapshots for key, value in item.vectors.items()},
            query_vector=query_vector,
            rerank_limit=self.settings.rerank_limit,
            compressor=compressor,
        )[:limit]
        originals = {chunk.id: chunk for chunk in chunks}
        selected = [
            originals[hit.id].model_copy(
                update={
                    "metadata": {
                        **originals[hit.id].metadata,
                        "retrieval_score": hit.metadata["retrieval_score"],
                    }
                }
            )
            for hit in hits
        ]
        expanded = list(selected)
        seen = {chunk.id for chunk in selected}
        for seed in selected:
            for chunk in chunks:
                same_section = (
                    chunk.metadata["document_id"] == seed.metadata["document_id"]
                    and chunk.metadata["section_id"] == seed.metadata["section_id"]
                )
                nearby = (
                    abs(chunk.metadata["ordinal"] - seed.metadata["ordinal"])
                    <= self.settings.expansion
                )
                if (
                    same_section
                    and (self.settings.expand_parent or nearby)
                    and chunk.id not in seen
                ):
                    seen.add(chunk.id)
                    expanded.append(
                        chunk.model_copy(
                            update={
                                "metadata": {
                                    **chunk.metadata,
                                    "retrieval_score": seed.metadata["retrieval_score"],
                                }
                            }
                        )
                    )
        evidence, context, omitted = assemble_evidence(expanded, self.settings.context_budget)
        return {
            "status": "ok" if evidence else "budget_exceeded" if expanded else "no_evidence",
            "mode": "hybrid" if self.settings.embedding_model else "bm25",
            "evidence": evidence,
            "context": context,
            "used_chunk_ids": [identifier for item in evidence for identifier in item["chunk_ids"]],
            "warnings": ["evidence_omitted_by_budget"] if omitted else [],
        }
