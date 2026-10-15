"""记忆检索策略：按完整笔记匹配，分词和排序规则独立于知识库。"""

import re
from collections.abc import Sequence

from langchain_core.documents import Document

from ..retrieval.lexical import lexical_search
from ..retrieval.strategy import RetrievalHit
from .models import MemoryRecord


def memory_terms(text: str) -> list[str]:
    """英文保留单词和标识符，中文按相邻双字切分，不调用模型。"""
    tokens = re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]+", text.casefold())
    return [
        part
        for token in tokens
        for part in (
            [token]
            if not re.fullmatch(r"[\u4e00-\u9fff]+", token) or len(token) == 1
            else [token[i : i + 2] for i in range(len(token) - 1)]
        )
    ]


class MemoryRetrievalStrategy:
    """对完整笔记做 BM25Plus 排序，不扩展相邻记录，也不执行额外模型请求。"""

    def retrieve(
        self, query: str, candidates: Sequence[MemoryRecord], *, limit: int
    ) -> tuple[RetrievalHit, ...]:
        query_terms = set(memory_terms(query))
        matches = [r for r in candidates if query_terms.intersection(memory_terms(r.content))]
        documents = lexical_search(
            [Document(id=r.id, page_content=r.content) for r in matches],
            query,
            limit,
            memory_terms,
        )
        return tuple(
            RetrievalHit(id=doc.id, score=float(doc.metadata["bm25_score"]))
            for doc in documents
            if doc.id is not None
        )
