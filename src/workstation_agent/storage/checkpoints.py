"""声明核心调用方实际需要的检查点和会话锁接口，不依赖数据库实现。"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import Any, BinaryIO, Protocol

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
)
from langgraph.checkpoint.serde.base import SerializerProtocol


class SessionBackend(Protocol):
    """使用官方类型保存和读取检查点，另提供本机会话互斥锁。"""

    @property
    def serde(self) -> SerializerProtocol: ...
    def acquire(self, thread_id: str) -> BinaryIO: ...
    def get_tuple(self, config: RunnableConfig) -> CheckpointTuple | None: ...
    def list(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> Iterator[CheckpointTuple]: ...
    def put(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig: ...
    def put_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None: ...
    def delete_thread(self, thread_id: str) -> None: ...
