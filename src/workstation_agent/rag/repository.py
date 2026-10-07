"""定义文档存储接口和内存实现。"""

from __future__ import annotations

from typing import Protocol

from .models import Document, TextChunk


class DocumentRepository(Protocol):
    """约定文档和文本片段的存取方法。"""
    def upsert_document(self, document: Document, chunks: tuple[TextChunk, ...]) -> None: ...
    def all_chunks(self) -> tuple[tuple[Document, TextChunk], ...]: ...


class InMemoryDocumentRepository:
    """在内存中保存文档及其片段。"""
    def __init__(self) -> None:
        self._documents: dict[str, Document] = {}
        self._chunks: dict[str, tuple[TextChunk, ...]] = {}

    def upsert_document(self, document: Document, chunks: tuple[TextChunk, ...]) -> None:
        """新增或替换文档及其全部片段。"""
        if any(chunk.document_id != document.id for chunk in chunks):
            raise ValueError("all chunks must belong to the document")
        self._documents[document.id] = document
        self._chunks[document.id] = chunks

    def all_chunks(self) -> tuple[tuple[Document, TextChunk], ...]:
        """返回全部文档与片段的对应关系。"""
        return tuple(
            (document, chunk)
            for document_id, document in self._documents.items()
            for chunk in self._chunks.get(document_id, ())
        )
