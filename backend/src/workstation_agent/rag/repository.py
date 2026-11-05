"""定义索引快照边界，不提供独立文档或关键词仓库兼容接口。"""

from threading import Lock
from typing import Protocol

from .index import IndexSnapshot, IndexState, validate_snapshot
from ..tools.contracts import ToolError


class IndexRepository(Protocol):
    """按同一读取快照管理原子发布的索引版本。"""

    def state(self) -> IndexState: ...
    def get(self, source_uri: str) -> IndexSnapshot | None: ...
    def revision(self, source_uri: str) -> int: ...
    def publish(self, snapshot: IndexSnapshot, *, expected_revision: int | None = None) -> None: ...
    def delete(self, source_uri: str) -> bool: ...


class InMemoryIndexRepository:
    """只保存一套完整索引快照，隔离调用方对框架文档的修改。"""

    def __init__(self) -> None:
        self._snapshots: dict[str, IndexSnapshot] = {}
        self._revisions: dict[str, int] = {}
        self._lock = Lock()

    def state(self) -> IndexState:
        return IndexState(
            tuple(self._snapshots[key].model_copy(deep=True) for key in sorted(self._snapshots))
        )

    def get(self, source_uri: str) -> IndexSnapshot | None:
        snapshot = self._snapshots.get(source_uri)
        return snapshot.model_copy(deep=True) if snapshot is not None else None

    def revision(self, source_uri: str) -> int:
        """读取来源的发布或删除版本。"""
        return self._revisions.get(source_uri, 0)

    def publish(self, snapshot: IndexSnapshot, *, expected_revision: int | None = None) -> None:
        """目标仍处于预期版本时发布完整快照。"""
        validate_snapshot(snapshot)
        source = snapshot.source.source_uri
        with self._lock:
            revision = self.revision(source)
            if expected_revision is not None and revision != expected_revision:
                raise ToolError("索引已变化，请重新准备", code="version_conflict")
            self._snapshots[source] = snapshot.model_copy(deep=True)
            self._revisions[source] = revision + 1

    def delete(self, source_uri: str) -> bool:
        """删除快照并保留来源版本。"""
        with self._lock:
            if self._snapshots.pop(source_uri, None) is None:
                return False
            self._revisions[source_uri] = self.revision(source_uri) + 1
            return True
