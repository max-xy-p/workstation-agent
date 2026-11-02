"""编排框架文档、索引快照与证据；副作用由工具执行边界授权。"""

from hashlib import sha256
from typing import Literal, NotRequired, TypedDict

from langchain_core.documents import Document

from ..context.evidence import Evidence, assemble_evidence
from ..rag.backend import RagBackend, token_upper_bound
from ..rag.chunking.contracts import (
    ChunkBudgetExceeded,
    ChunkingStrategy,
    InvalidChunkingResult,
)
from ..rag.chunking.structured import StructuredChunkingStrategy
from ..rag.chunking.validation import validate_chunks
from ..rag.config import RagSettings
from ..rag.identifiers import stable_id
from ..rag.index import (
    INDEX_TEXT_VERSION,
    IndexSnapshot,
    IndexState,
    index_text,
    validate_snapshot,
    validate_vectors,
)
from ..rag.models import SourceDocument
from ..rag.repository import IndexRepository
from ..rag.retriever import KnowledgeRetrievalStrategy
from ..retrieval.strategy import RetrievalStrategy, validate_hits
from ..tools.contracts import ToolError

_CHUNK_BUDGET_ERROR = "RAG chunk budget exceeded"


class IngestResult(TypedDict):
    """描述文档摄取结果；未变化的文档不重复生成索引。"""

    status: Literal["unchanged", "indexed"]
    document_id: str
    chunks: int
    checksum: NotRequired[str]
    mode: NotRequired[str]


class IndexStatus(TypedDict):
    """记录索引数量和是否需要重新摄取。"""

    status: str
    documents: int
    chunks: int
    incompatible_documents: int
    invalid_documents: int
    obsolete_sources: list[str]
    mode: str


class DeleteResult(TypedDict):
    """说明指定文档的索引是否已删除。"""

    status: Literal["deleted", "not_found"]


class SearchResult(TypedDict):
    """返回实际采用的原文证据、引用 ID 和预算提示。"""

    status: str
    mode: str
    evidence: list[Evidence]
    context: str
    used_chunk_ids: list[str]
    warnings: list[str]


class RagApplication:
    """构建完整快照后原子发布，处理链统一使用框架 Document。"""

    def __init__(
        self,
        repository: IndexRepository,
        settings: RagSettings,
        backend: RagBackend | None = None,
        *,
        retrieval: RetrievalStrategy[IndexSnapshot] | None = None,
        chunking: ChunkingStrategy | None = None,
    ) -> None:
        self.repository: IndexRepository = repository
        self.settings = settings
        self.backend: RagBackend | None = backend
        self.retrieval: RetrievalStrategy[IndexSnapshot] = (
            retrieval if retrieval is not None else KnowledgeRetrievalStrategy(settings, backend)
        )
        self.chunker: ChunkingStrategy = (
            chunking if chunking is not None else StructuredChunkingStrategy(settings)
        )

    @property
    def fingerprint(self) -> str:
        """配置和管线版本变化时要求重新摄取，不转换旧处理结果。"""
        parts = (
            *self.chunker.signature,
            "terms-v1",
            INDEX_TEXT_VERSION,
            self.backend.fingerprint if self.backend else "lexical",
        )
        return sha256(repr(parts).encode()).hexdigest()

    def ingest(self, source: str, text: str, metadata: dict[str, str], *, title: str | None = None,
               media_type: Literal["text/markdown", "text/plain"] | None = None) -> IngestResult:
        """处理已授权读取的文本；发布失败时保留已有版本。"""
        if len(text.encode("utf-8")) > self.settings.max_file_bytes:
            raise ToolError("文档正文超过摄取预算", code="resource_limit")
        if "\0" in text:
            raise ToolError("文档正文包含二进制内容", code="unsupported_format")
        title = title if title is not None else source
        media_type = media_type or ("text/markdown" if source.lower().endswith(".md") else "text/plain")
        checksum = sha256(text.encode("utf-8")).hexdigest()
        previous = self.repository.get(source)
        if (
            previous
            and previous.source.checksum == checksum
            and previous.fingerprint == self.fingerprint
            and previous.source.metadata == metadata
            and previous.source.title == title
            and previous.source.media_type == media_type
        ):
            return {
                "status": "unchanged",
                "document_id": previous.source.id,
                "chunks": len(previous.chunks),
            }
        document = SourceDocument(
            id=stable_id("doc", source),
            source_uri=source,
            title=title,
            checksum=checksum,
            media_type=media_type,
            metadata=metadata,
        )
        chunks = self._split(document, text)
        self._check_ingestion_budget(chunks, source)
        chunks = self._contextualize(document, text, chunks)
        vectors = self._embed(chunks)
        snapshot = IndexSnapshot(
            source=document,
            chunks=chunks,
            vectors=vectors,
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

    def _split(self, document: SourceDocument, text: str) -> tuple[Document, ...]:
        """调用策略后执行统一校验，将策略错误转换为安全的工具错误。"""
        try:
            chunks = self.chunker.split(document, text)
            validate_chunks(document, text, chunks, self.settings)
            return chunks
        except ChunkBudgetExceeded:
            raise ToolError(_CHUNK_BUDGET_ERROR, code="resource_limit") from None
        except InvalidChunkingResult:
            raise ToolError("分块策略返回了无效结果", code="invalid_chunking_output") from None

    def _check_ingestion_budget(self, chunks: tuple[Document, ...], source: str) -> None:
        """发布前检查模型次数和全库块数，超限时不修改旧索引。"""
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
            raise ToolError(_CHUNK_BUDGET_ERROR, code="resource_limit")

    def _contextualize(
        self, document: SourceDocument, text: str, chunks: tuple[Document, ...]
    ) -> tuple[Document, ...]:
        """为每个块附加受控模型生成的上下文，原文保持不变。"""
        backend = self.backend
        if not self.settings.context_model or backend is None:
            return chunks
        enriched: list[Document] = []
        for chunk in chunks:
            section = [
                item
                for item in chunks
                if item.metadata["section_id"] == chunk.metadata["section_id"]
            ]
            background = text[
                section[0].metadata["source_start"] : section[-1].metadata["source_end"]
            ]
            prefix = backend.contextualize(document.title, background, chunk.page_content)
            if not isinstance(prefix, str) or not prefix.strip() or len(prefix) > 512:
                raise ToolError("invalid contextual prefix", code="invalid_model_output")
            enriched.append(
                chunk.model_copy(
                    update={
                        "metadata": {
                            **chunk.metadata,
                            "context_prefix": chunk.metadata["context_prefix"] + "\n" + prefix,
                            "context_provenance": backend.fingerprint,
                        }
                    }
                )
            )
        return tuple(enriched)

    def _embed(self, chunks: tuple[Document, ...]) -> dict[str, tuple[float, ...]]:
        """先检查索引文本预算，再按已授权配置生成并校验向量。"""
        texts = [index_text(chunk) for chunk in chunks]
        if any(token_upper_bound(value) > self.settings.input_budget for value in texts):
            raise ToolError("RAG index text exceeds input budget", code="resource_limit")
        backend = self.backend
        if not self.settings.embedding_model or backend is None:
            return {}
        vectors: list[list[float]] = []
        for index in range(0, len(texts), 16):
            vectors.extend(backend.embed(texts[index : index + 16]))
        validate_vectors(vectors, len(chunks))
        return {
            chunk.id: tuple(vector)
            for chunk, vector in zip(chunks, vectors, strict=True)
            if chunk.id is not None
        }

    def status(self) -> IndexStatus:
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
        status = "ready"
        if not documents:
            status = "missing"
        elif incompatible or invalid:
            status = "needs_reindex"
        return {
            "status": status,
            "documents": documents,
            "chunks": sum(len(item.chunks) for item in state.snapshots),
            "incompatible_documents": incompatible,
            "invalid_documents": invalid,
            "obsolete_sources": list(state.obsolete_sources),
            "mode": "hybrid" if self.settings.embedding_model else "bm25",
        }

    def delete(self, source: str) -> DeleteResult:
        """删除指定来源的索引快照，保留原文件。"""
        return {"status": "deleted" if self.repository.delete(source) else "not_found"}

    def search(self, query: str, filters: dict[str, str], limit: int) -> SearchResult:
        """检索返回框架文档，通过块 ID 回到原文，再进行预算内扩展。"""
        if not query.strip():
            raise ToolError("query must not be blank", code="invalid_arguments")
        if limit < 1:
            raise ToolError("检索数量必须大于零", code="invalid_arguments")
        snapshots = self._matching_snapshots(self.repository.state(), filters)
        chunks = [chunk for item in snapshots for chunk in item.chunks]
        if len(chunks) > self.settings.max_chunks:
            raise ToolError(_CHUNK_BUDGET_ERROR, code="resource_limit")
        if not chunks:
            return self._search_result([], [], "", False)
        originals = {chunk.id: chunk for chunk in chunks if chunk.id is not None}
        hits = validate_hits(
            self.retrieval.retrieve(query, tuple(snapshots), limit=limit), set(originals), limit
        )
        selected = [
            originals[hit.id].model_copy(
                update={
                    "metadata": {
                        **originals[hit.id].metadata,
                        "retrieval_score": hit.score,
                    }
                }
            )
            for hit in hits
        ]
        expanded = self._expand(selected, chunks)
        evidence, context, omitted = assemble_evidence(expanded, self.settings.context_budget)
        return self._search_result(evidence, expanded, context, omitted)

    def _matching_snapshots(
        self, state: IndexState, filters: dict[str, str]
    ) -> list[IndexSnapshot]:
        """先过滤可见文档，再拒绝格式或配置已变化的命中范围。"""
        obsolete = [
            source
            for source in state.obsolete_sources
            if "source_uri" not in filters or filters["source_uri"] == source
        ]
        snapshots = [item for item in state.snapshots if self._matches(item, filters)]
        if obsolete or any(item.fingerprint != self.fingerprint for item in snapshots):
            raise ToolError(
                "RAG index format or configuration changed; re-ingest matching documents",
                code="index_incompatible",
            )
        return snapshots

    @staticmethod
    def _matches(item: IndexSnapshot, filters: dict[str, str]) -> bool:
        attributes = {
            **item.source.metadata,
            "source_uri": item.source.source_uri,
            "document_id": item.source.id,
            "media_type": item.source.media_type,
        }
        return all(attributes.get(key) == value for key, value in filters.items())

    def _expand(self, selected: list[Document], chunks: list[Document]) -> list[Document]:
        """命中块在前，同章节邻块随后加入；每个块只注入一次。"""
        expanded = list(selected)
        seen = {chunk.id for chunk in selected}
        for seed in selected:
            for chunk in chunks:
                if chunk.id in seen or not self._is_neighbor(seed, chunk):
                    continue
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
        return expanded

    def _is_neighbor(self, seed: Document, chunk: Document) -> bool:
        same_section = (
            chunk.metadata["document_id"] == seed.metadata["document_id"]
            and chunk.metadata["section_id"] == seed.metadata["section_id"]
        )
        nearby = (
            abs(chunk.metadata["ordinal"] - seed.metadata["ordinal"]) <= self.settings.expansion
        )
        return same_section and (self.settings.expand_parent or nearby)

    def _search_result(
        self, evidence: list[Evidence], expanded: list[Document], context: str, omitted: bool
    ) -> SearchResult:
        status = "no_evidence"
        if evidence:
            status = "ok"
        elif expanded:
            status = "budget_exceeded"
        return {
            "status": status,
            "mode": "hybrid" if self.settings.embedding_model else "bm25",
            "evidence": evidence,
            "context": context,
            "used_chunk_ids": [identifier for item in evidence for identifier in item["chunk_ids"]],
            "warnings": ["evidence_omitted_by_budget"] if omitted else [],
        }
