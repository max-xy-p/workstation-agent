"""复用现成 BM25 与官方 Retriever 扩展点，不接触存储和网络。"""

from collections.abc import Callable, Sequence

from langchain_core.callbacks import CallbackManagerForRetrieverRun
from langchain_core.documents import Document
from langchain_core.retrievers import BaseRetriever
from rank_bm25 import BM25Plus


class _BM25Retriever(BaseRetriever):
    """补充零命中排除和稳定排序，评分交给成熟组件。"""

    docs: list[Document]
    vocabulary: list[frozenset[str]]
    vectorizer: BM25Plus
    k: int
    tokenizer: Callable[[str], list[str]]

    def _get_relevant_documents(
        self, query: str, *, run_manager: CallbackManagerForRetrieverRun
    ) -> list[Document]:
        query_terms = list(dict.fromkeys(self.tokenizer(query)))
        scores = self.vectorizer.get_scores(query_terms)
        matching = [i for i, words in enumerate(self.vocabulary) if words.intersection(query_terms)]
        matching.sort(key=lambda i: (-scores[i], i))
        return [
            self.docs[i].model_copy(
                update={"metadata": {**self.docs[i].metadata, "bm25_score": float(scores[i])}}
            )
            for i in matching[: self.k]
        ]


def lexical_search(
    documents: Sequence[Document],
    query: str,
    limit: int,
    tokenizer: Callable[[str], list[str]],
) -> list[Document]:
    """在调用方已过滤的文档上检索，记忆与 RAG 不共享数据。"""
    usable = [(doc, tokenizer(doc.page_content)) for doc in documents]
    usable = [(doc, tokens) for doc, tokens in usable if tokens]
    if not usable:
        return []
    docs, corpus = zip(*usable, strict=True)
    return _BM25Retriever(
        docs=list(docs),
        vocabulary=[frozenset(tokens) for tokens in corpus],
        vectorizer=BM25Plus(list(corpus), k1=1.5, b=0.75, delta=1),
        tokenizer=tokenizer,
        k=limit,
    ).invoke(query)
