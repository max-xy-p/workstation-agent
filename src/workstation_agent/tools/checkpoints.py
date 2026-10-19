"""按明确的方法授权官方 Checkpointer 操作，不使用字符串分发。"""

from __future__ import annotations

import asyncio
import hashlib
import re
from collections.abc import AsyncIterator, Iterator, Sequence
from contextlib import contextmanager
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
)

from ..memory.models import MemoryAccess
from ..safety.permissions import ApprovalPolicy, PermissionLevel
from ..storage.checkpoints import SessionBackend
from .effects import authorized_operation


class AuthorizedCheckpointer(BaseCheckpointSaver):
    """绑定可信身份命名空间，所有写入和会话锁都经过工具权限检查。"""

    def __init__(
        self, backend: SessionBackend, access: MemoryAccess, policy: ApprovalPolicy
    ) -> None:
        super().__init__(serde=backend.serde)
        self.backend: SessionBackend = backend
        self.access: MemoryAccess = access
        self.policy: ApprovalPolicy = policy

    def config(self, session_id: str) -> RunnableConfig:
        """外部会话名映射为当前身份和范围内的 thread_id。"""
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", session_id):
            raise ValueError("会话标识只能包含字母、数字、下划线和连字符")
        namespace = self.access.scope().namespace + ":" + session_id
        return {"configurable": {"thread_id": hashlib.sha256(namespace.encode()).hexdigest()}}

    def get_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        """授权后直接调用后端 get_tuple，保留官方参数和返回类型。"""
        return authorized_operation(
            "session_get_tuple",
            PermissionLevel.SAFE_READ,
            self.policy,
            lambda: self.backend.get_tuple(config),
        )

    def list(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> Iterator[CheckpointTuple]:
        """授权后直接调用后端 list，保留官方参数和返回类型。"""
        rows = authorized_operation(
            "session_list",
            PermissionLevel.SAFE_READ,
            self.policy,
            lambda: list(self.backend.list(config, filter=filter, before=before, limit=limit)),
        )
        yield from rows

    def put(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        """授权后直接调用后端 put，保留官方参数和返回类型。"""
        return authorized_operation(
            "session_put",
            PermissionLevel.CONTROLLED_WRITE,
            self.policy,
            lambda: self.backend.put(config, checkpoint, metadata, new_versions),
        )

    def put_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        """授权后直接调用后端 put_writes，保留官方参数和返回类型。"""
        return authorized_operation(
            "session_put_writes",
            PermissionLevel.CONTROLLED_WRITE,
            self.policy,
            lambda: self.backend.put_writes(config, writes, task_id, task_path),
        )

    @contextmanager
    def lease(self, config: RunnableConfig, *, deleting: bool = False) -> Iterator[None]:
        """整个运行和清除互斥；权限检查通过后才创建或获取本机文件锁。"""
        name = "session_lease_delete" if deleting else "session_lease"
        permission = PermissionLevel.DESTRUCTIVE if deleting else PermissionLevel.CONTROLLED_WRITE
        handle = authorized_operation(
            name,
            permission,
            self.policy,
            lambda: self.backend.acquire(config.get("configurable", {})["thread_id"]),
        )
        try:
            yield
        finally:
            # 释放自己已经持有的句柄不需要重新询问权限，避免策略变化导致锁无法释放。
            handle.close()

    def delete_thread(self, thread_id: str) -> None:
        """持有会话锁后删除当前 thread_id 的检查点。"""
        with self.lease({"configurable": {"thread_id": thread_id}}, deleting=True):
            authorized_operation(
                "session_delete_thread",
                PermissionLevel.DESTRUCTIVE,
                self.policy,
                lambda: self.backend.delete_thread(thread_id),
            )

    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        """在线程中调用有完整类型的同步 get_tuple 方法。"""
        return await asyncio.to_thread(lambda: self.get_tuple(config))

    async def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        """在线程中收集同步结果，再按官方异步迭代接口返回。"""
        rows = await asyncio.to_thread(
            lambda: list(self.list(config, filter=filter, before=before, limit=limit))
        )
        for row in rows:
            yield row

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        """在线程中调用有完整类型的同步 put 方法。"""
        return await asyncio.to_thread(lambda: self.put(config, checkpoint, metadata, new_versions))

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        """在线程中调用有完整类型的同步 put_writes 方法。"""
        return await asyncio.to_thread(lambda: self.put_writes(config, writes, task_id, task_path))

    async def adelete_thread(self, thread_id: str) -> None:
        """在线程中调用有完整类型的同步 delete_thread 方法。"""
        return await asyncio.to_thread(lambda: self.delete_thread(thread_id))
