"""定义记忆存储接口和内存实现。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol

from .models import MemoryQuery, MemoryRecord


class MemoryRepository(Protocol):
    """约定记忆的保存、检索和删除方法。"""
    def save(self, record: MemoryRecord) -> None: ...
    def search(self, query: MemoryQuery) -> tuple[MemoryRecord, ...]: ...
    def delete(self, memory_id: str) -> bool: ...


class InMemoryMemoryRepository:
    """在内存中保存记忆，并按条件过滤和排序。"""

    def __init__(self) -> None:
        self._records: dict[str, MemoryRecord] = {}

    def save(self, record: MemoryRecord) -> None:
        """按标识新增或替换记忆。"""
        self._records[record.id] = record

    def search(self, query: MemoryQuery) -> tuple[MemoryRecord, ...]:
        """过滤过期记录，按词项命中和权重排序。"""
        now = datetime.now(UTC)
        terms = {term.casefold() for term in query.text.split() if term}
        records = [
            record
            for record in self._records.values()
            if (record.expires_at is None or record.expires_at > now)
            and (query.scope is None or record.scope == query.scope)
            and (query.scope_id is None or record.scope_id == query.scope_id)
            and (not query.kinds or record.kind in query.kinds)
        ]

        def score(record: MemoryRecord) -> tuple[float, datetime]:
            content = record.content.casefold()
            lexical = sum(term in content for term in terms)
            return lexical + record.importance + record.confidence, record.created_at

        records.sort(key=score, reverse=True)
        return tuple(records[: query.limit])

    def delete(self, memory_id: str) -> bool:
        """删除指定记忆，返回是否存在该记录。"""
        return self._records.pop(memory_id, None) is not None
