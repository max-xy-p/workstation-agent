"""定义索引快照边界，不提供独立文档或关键词仓库兼容接口。"""

from typing import Protocol

from .index import IndexSnapshot, IndexState, validate_snapshot


class IndexRepository(Protocol):
    """按同一读取快照管理原子发布的索引版本。"""

    def state(self) -> IndexState: ...
    def get(self, source_uri: str) -> IndexSnapshot | None: ...
    def publish(self, snapshot: IndexSnapshot) -> None: ...
    def delete(self, source_uri: str) -> bool: ...


class InMemoryIndexRepository:
    """只保存一套完整索引快照，隔离调用方对框架文档的修改。"""

    def __init__(self):
        self._snapshots: dict[str, IndexSnapshot] = {}

    def state(self) -> IndexState:
        return IndexState(
            tuple(self._snapshots[key].model_copy(deep=True) for key in sorted(self._snapshots))
        )

    def get(self, source_uri: str) -> IndexSnapshot | None:
        snapshot = self._snapshots.get(source_uri)
        return snapshot.model_copy(deep=True) if snapshot is not None else None

    def publish(self, snapshot: IndexSnapshot) -> None:
        validate_snapshot(snapshot)
        self._snapshots[snapshot.source.source_uri] = snapshot.model_copy(deep=True)

    def delete(self, source_uri: str) -> bool:
        return self._snapshots.pop(source_uri, None) is not None
