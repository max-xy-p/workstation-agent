"""提供记忆保存、检索和删除入口。"""

from __future__ import annotations

from .models import MemoryQuery, MemoryRecord
from .repository import MemoryRepository


class MemoryService:
    """检查记忆内容并调用存储接口。"""
    def __init__(self, repository: MemoryRepository) -> None:
        self.repository = repository

    def remember(self, record: MemoryRecord) -> MemoryRecord:
        """拒绝空内容并保存记忆。"""
        if not record.content.strip():
            raise ValueError("memory content must not be empty")
        self.repository.save(record)
        return record

    def search(self, query: MemoryQuery) -> tuple[MemoryRecord, ...]:
        """按查询条件检索记忆。"""
        return self.repository.search(query)

    def forget(self, memory_id: str) -> bool:
        """删除指定记忆。"""
        return self.repository.delete(memory_id)
