"""通过官方检索接口组合 BM25、向量检索和排名融合。"""

from __future__ import annotations

import math
from typing import Any

from langchain_classic.retrievers import ContextualCompressionRetriever, EnsembleRetriever
from langchain_core.documents import BaseDocumentCompressor
from langchain_core.embeddings import Embeddings
from langchain_core.runnables import RunnableLambda
from langchain_core.vectorstores import InMemoryVectorStore

from ..retrieval.lexical import lexical_search
from ..tools.contracts import ToolError
from .index import index_text, terms, validate_vectors


class _SnapshotEmbeddings(Embeddings):
    """仅装载已发布向量，查询重建内存索引时禁止再次调用模型。"""

    def __init__(self, texts, vectors):
        self.texts = texts
        self.vectors = vectors

    def embed_documents(self, texts):
        if texts != self.texts:
            raise ValueError("向量快照与文档不匹配")
        return [list(vector) for vector in self.vectors]

    def embed_query(self, text):
        raise RuntimeError("查询向量必须经授权模型工具提供")


class FrameworkReranker(BaseDocumentCompressor):
    """通过官方压缩器接口接入已授权重排模型，限制调用预算并校验分数。"""

    backend: Any
    max_model_calls: int
    prior_calls: int = 0

    def compress_documents(self, documents, query, callbacks=None):
        if not documents:
            return []
        if len(documents) + self.prior_calls > self.max_model_calls:
            raise ToolError("RAG model call budget exceeded", code="resource_limit")
        scores = self.backend.rerank(query, [doc.page_content for doc in documents])
        if len(scores) != len(documents) or any(
            type(score) not in (int, float) or not math.isfinite(score) or not 0 <= score <= 1
            for score in scores
        ):
            raise ToolError("invalid reranking scores", code="invalid_model_output")
        ranked = [
            doc.model_copy(update={"metadata": {**doc.metadata, "retrieval_score": score}})
            for doc, score in zip(documents, scores, strict=True)
            if score > 0
        ]
        return sorted(
            ranked,
            key=lambda doc: (-doc.metadata["retrieval_score"], doc.metadata["candidate_order"]),
        )


class HybridSearch:
    """在过滤后的快照上使用框架组件召回，按块 ID 融合。"""

    version = "native-retrieval-v3"

    @staticmethod
    def lexical(documents, query, limit):
        """用现成 BM25Plus 避免小语料高频词出现负 IDF，算法不在项目内实现。"""
        return lexical_search(documents, query, limit, terms)

    def retrieve(
        self,
        chunks,
        query,
        limit,
        vectors=None,
        query_vector=None,
        *,
        rerank_limit=30,
        compressor=None,
    ):
        """输入仅含已授权数据，组件内部不读取文件或访问网络。"""
        documents = [
            chunk.model_copy(
                update={
                    "page_content": index_text(chunk),
                    "metadata": {**chunk.metadata, "chunk_id": chunk.id, "candidate_order": i},
                }
            )
            for i, chunk in enumerate(chunks)
        ]
        lexical = self.lexical(documents, query, limit)
        if query_vector is None:
            ranked = [
                doc.model_copy(
                    update={
                        "metadata": {**doc.metadata, "retrieval_score": doc.metadata["bm25_score"]}
                    }
                )
                for doc in lexical
            ]
        else:
            stored_vectors = [vectors[chunk.id] for chunk in chunks]
            dimension = validate_vectors([query_vector], 1)
            validate_vectors(stored_vectors, len(chunks), dimension)
            store = InMemoryVectorStore(
                _SnapshotEmbeddings([doc.page_content for doc in documents], stored_vectors)
            )
            store.add_documents(documents, ids=[doc.id for doc in documents])
            dense_results = store.similarity_search_with_score_by_vector(
                list(query_vector), k=len(documents)
            )
            # 官方组件未规定同分顺序，项目按快照顺序处理完全相同的向量分数。
            dense_results.sort(key=lambda item: (-item[1], item[0].metadata["candidate_order"]))
            dense = [doc for doc, score in dense_results if score > 0][:limit]
            ensemble = EnsembleRetriever(
                retrievers=[
                    RunnableLambda(lambda _: lexical),
                    RunnableLambda(lambda _: dense),
                ],
                weights=[1.0, 1.0],
                c=60,
                id_key="chunk_id",
            )
            fused = ensemble.invoke(query)
            # 官方融合器只返回排序，展示分数取融合位置的倒数，不伪称为 RRF 原始分数。
            ranked = [
                doc.model_copy(update={"metadata": {**doc.metadata, "retrieval_score": 1.0 / rank}})
                for rank, doc in enumerate(fused, 1)
            ]
        ranked = ranked[:rerank_limit]
        if compressor is not None:
            ranked = list(
                ContextualCompressionRetriever(
                    base_retriever=RunnableLambda(lambda _: ranked),
                    base_compressor=compressor,
                ).invoke(query)
            )
        return ranked
