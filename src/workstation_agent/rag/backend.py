"""定义可替换的 RAG 模型能力，核心不判断供应商类型。"""

from typing import Protocol


class RagBackend(Protocol):
    """约定 embedding、块上下文和候选重排，调用方负责授权。"""

    fingerprint: str

    def embed(self, texts: list[str]) -> list[list[float]]: ...
    def contextualize(self, title: str, background: str, content: str) -> str: ...
    def rerank(self, query: str, texts: list[str]) -> list[float]: ...


def token_upper_bound(text: str) -> int:
    """用 UTF-8 字节数保守计量预算，不声称是模型 tokenizer 的精确计数。"""
    return len(text.encode("utf-8"))
