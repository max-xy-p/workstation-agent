"""长期记忆持久化协议，不暴露具体数据库。"""

from typing import Protocol

from .models import MemoryRecord, MemoryScope


class MemoryRepository(Protocol):
    """读取范围快照，原子提交版本化变更，并保留无正文的处理水位。"""

    def records(self, scopes: tuple[MemoryScope, ...]) -> tuple[MemoryRecord, ...]: ...
    def generation(self, scope: MemoryScope) -> int: ...
    def processed(self, scope: MemoryScope, batch_id: str) -> bool: ...
    def apply(
        self,
        records: tuple[MemoryRecord, ...],
        expected: dict[str, int | None],
        scope: MemoryScope,
        *,
        generation: int | None = None,
        batch_id: str | None = None,
    ) -> bool: ...
    def delete(self, memory_id: str, scope: MemoryScope, version: int) -> bool: ...
