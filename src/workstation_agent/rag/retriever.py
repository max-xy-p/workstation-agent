"""定义知识检索接口并提供关键词检索。"""

from __future__ import annotations

from typing import Protocol

from .models import RetrievalQuery, RetrievedChunk
from .repository import DocumentRepository


class Retriever(Protocol):
    """约定知识片段的检索方法。"""
    def retrieve(self, query: RetrievalQuery) -> tuple[RetrievedChunk, ...]: ...


class KeywordRetriever:
    """按关键词出现次数检索并排序知识片段。"""

    def __init__(self, repository: DocumentRepository) -> None:
        self.repository = repository

    def retrieve(self, query: RetrievalQuery) -> tuple[RetrievedChunk, ...]:
        """按文档属性过滤，返回匹配的知识片段。"""
        terms = {term.casefold() for term in query.text.split() if term}
        results: list[RetrievedChunk] = []
        for document, chunk in self.repository.all_chunks():
            if any(document.metadata.get(key) != value for key, value in query.filters.items()):
                continue
            content = chunk.content.casefold()
            hits = sum(content.count(term) for term in terms)
            if hits:
                results.append(
                    RetrievedChunk(
                        chunk=chunk,
                        score=float(hits),
                        source_uri=document.source_uri,
                        title=document.title,
                    )
                )
        results.sort(key=lambda result: result.score, reverse=True)
        return tuple(results[: query.limit])
