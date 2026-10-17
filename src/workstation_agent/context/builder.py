"""通过受控检索获取候选；所有进入模型的记忆和 RAG 内容在此格式化。"""

from __future__ import annotations

import json
from typing import Protocol

from langchain_core.documents import Document
from langchain_core.runnables import Runnable

from ..memory.models import MemoryRecord
from .evidence import assemble_evidence
from .state import ContextItem


class MemoryReader(Protocol):
    """只依赖已按身份和权限过滤的记忆查询，不接触数据库。"""

    def search(self, text: str) -> tuple[MemoryRecord, ...]: ...


class ContextBuilder:
    """获取带版本的候选，最终选择与 Token 检查由请求规划器完成。"""

    def __init__(
        self,
        *,
        memory: MemoryReader | None = None,
        retriever: Runnable[str, list[Document]] | None = None,
        max_context_chars: int = 20000,
        max_memory_chars: int = 4000,
    ) -> None:
        if any(type(n) is not int or n < 1 for n in (max_context_chars, max_memory_chars)):
            raise ValueError("上下文字符预算必须为正整数")
        self.memory = memory
        self.retriever = retriever
        self.max_context_chars = max_context_chars
        self.max_memory_chars = max_memory_chars

    def retrieve(self, query: str, *, memory_only: bool = False) -> list[ContextItem]:
        """返回完整候选；字符上限仅限制检索资源，不能代替最终请求预算。"""
        items: list[ContextItem] = []
        remaining = min(self.max_memory_chars, self.max_context_chars)
        if self.memory is not None:
            for record in self.memory.search(query):
                text = f"[{record.id} v{record.version}] {record.content}"
                if len(text) > remaining:
                    continue
                remaining -= len(text)
                items.append(
                    ContextItem(
                        record.id,
                        "memory",
                        text,
                        record.scope.namespace,
                        str(record.version),
                        record.expires_at,
                    )
                )
        if self.retriever is not None and not memory_only:
            evidence, _, _ = assemble_evidence(
                self.retriever.invoke(query),
                self.max_context_chars - sum(len(item.content) for item in items),
                measure=len,
            )
            for group in evidence:
                items.append(
                    ContextItem(
                        ",".join(group["chunk_ids"]),
                        "rag",
                        f"[{group['citation']}]\n{group['content']}",
                        group["source_uri"],
                        group["checksum"],
                    )
                )
        return list({item.key: item for item in items}.values())


def serialize_memory_records(records: tuple[MemoryRecord, ...]) -> str:
    """显式记忆查询返回数据标记，不能把检索正文当作系统指令。"""
    return json.dumps(
        {
            "kind": "memory_data",
            "records": [record.model_dump(mode="json") for record in records],
        },
        ensure_ascii=False,
    )
