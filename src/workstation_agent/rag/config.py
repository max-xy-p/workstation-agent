"""定义 RAG 的预算与模型配置，不在加载配置时产生副作用。"""

import os
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RagSettings:
    """限制文档摄取、检索和外部模型开销。"""

    chunk_size: int = 1000
    chunk_overlap: int = 100
    max_file_bytes: int = 1_000_000
    max_chunks: int = 10_000
    context_budget: int = 8000
    recall_limit: int = 20
    rerank_limit: int = 30
    search_limit: int = 6
    expansion: int = 1
    expand_parent: bool = False
    max_search_calls: int = 6
    input_budget: int = 8192
    max_model_calls: int = 64
    embedding_model: str | None = None
    context_model: str | None = None
    allow_remote: bool = False

    def __post_init__(self):
        for name in (
            "chunk_size",
            "max_file_bytes",
            "max_chunks",
            "context_budget",
            "recall_limit",
            "rerank_limit",
            "search_limit",
            "max_search_calls",
            "input_budget",
            "max_model_calls",
        ):
            if type(getattr(self, name)) is not int or getattr(self, name) < 1:
                raise ValueError("RAG 预算必须为正整数")
        if type(self.chunk_overlap) is not int or not 0 <= self.chunk_overlap < self.chunk_size:
            raise ValueError("RAG 重叠必须为非负整数且小于块大小")
        if self.chunk_size < 32 or type(self.expansion) is not int or not 0 <= self.expansion <= 2:
            raise ValueError("RAG 块大小或扩展范围无效")
        if type(self.expand_parent) is not bool:
            raise ValueError("RAG 父章节扩展必须为布尔值")
        if type(self.allow_remote) is not bool or not 1 <= self.search_limit <= 20:
            raise ValueError("RAG 出站授权或返回数量无效")

    @classmethod
    def from_env(cls):
        """读取 WORKSTATION_AGENT_RAG_*，不回显非法配置值。"""
        values = {}
        for name in cls.__dataclass_fields__:
            raw = os.getenv("WORKSTATION_AGENT_RAG_" + name.upper())
            if raw is None:
                continue
            if name.endswith("_model"):
                values[name] = raw or None
            elif name in {"allow_remote", "expand_parent"}:
                if raw.lower() not in {"true", "false"}:
                    raise ValueError("RAG 出站授权必须为 true 或 false")
                values[name] = raw.lower() == "true"
            else:
                try:
                    values[name] = int(raw)
                except ValueError:
                    raise ValueError("RAG 数值配置无效") from None
        return cls(**values)
