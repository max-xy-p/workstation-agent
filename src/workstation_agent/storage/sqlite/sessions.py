"""官方 SQLite Checkpointer 的本机适配，不保存原始工具输出。"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import fields, is_dataclass, replace
from pathlib import Path
from threading import RLock
from typing import Any, BinaryIO

from langchain_core.messages import BaseMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
)
from langgraph.checkpoint.serde.base import SerializerProtocol
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite import SqliteSaver

from ...memory.service import redact
from ...tools.contracts import ToolError

_UNINITIALIZED_SESSION = "会话存储未初始化"


class SessionSerializer:
    """保留框架消息协议，持久化时清除工具正文并脱敏已知秘密。"""

    def __init__(self, secrets: tuple[str | None, ...] = ()) -> None:
        self.secrets = secrets
        self.serializer = JsonPlusSerializer()

    def _clean(self, value: Any) -> Any:
        if isinstance(value, ToolMessage):
            return value.model_copy(
                update={
                    "content": "[工具结果未持久化，需要时重新读取]",
                    "artifact": None,
                    "additional_kwargs": {},
                    "response_metadata": {},
                }
            )
        if isinstance(value, BaseMessage):
            cleaned_fields = self._clean(value.model_dump())
            return type(value).model_validate(cleaned_fields)
        if is_dataclass(value) and not isinstance(value, type):
            # 框架 Interrupt 等记录也可能包含工具参数，必须经过同一脱敏路径。
            return replace(
                value,
                **{
                    item.name: self._clean(getattr(value, item.name))
                    for item in fields(value)
                    if item.init
                },
            )
        if isinstance(value, dict):
            return {key: self._clean(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._clean(item) for item in value]
        if isinstance(value, tuple):
            return tuple(self._clean(item) for item in value)
        if isinstance(value, str):
            return redact(value, self.secrets)
        return value

    def dumps_typed(self, obj: Any) -> tuple[str, bytes]:
        """框架所有 channel 与 pending write 都使用同一脱敏路径。"""
        return self.serializer.dumps_typed(self._clean(obj))

    def loads_typed(self, data: tuple[str, bytes]) -> Any:
        return self.serializer.loads_typed(data)


class SQLiteSessionBackend:
    """按调用打开官方存储；未初始化时读取不创建数据库。"""

    def __init__(
        self, path: Path, workspace: Path, *, secrets: tuple[str | None, ...] = ()
    ) -> None:
        self._lock = RLock()
        self.path, self.workspace = path.absolute(), workspace.resolve()
        self.serde: SerializerProtocol = SessionSerializer(secrets)

    def acquire(self, thread_id: str) -> BinaryIO:
        """持有本机进程锁，避免同一会话并发执行或清除后被旧执行重建。"""
        path = self.path.parent / "session-locks" / (thread_id + ".lock")
        if not path.resolve().is_relative_to(self.workspace):
            raise ToolError("会话锁路径越界", code="path_outside_workspace")
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = path.open("a+b")
        try:
            handle.seek(0, 2)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            import os

            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            raise ToolError("会话正在执行，请稍后重试", code="session_busy") from None
        return handle

    @contextmanager
    def _saver(self, *, write: bool) -> Iterator[SqliteSaver | None]:
        """串行执行同一后端的建表检查和读写，避免多连接争用初始化锁。"""
        with self._lock, self._open_saver(write=write) as saver:
            yield saver

    @contextmanager
    def _open_saver(self, *, write: bool) -> Iterator[SqliteSaver | None]:
        """读取使用 SQLite 只读连接；仅授权写入允许建目录和执行框架迁移。"""
        if not self.path.resolve().is_relative_to(self.workspace):
            raise ToolError("会话数据路径越界", code="path_outside_workspace")
        if not self.path.exists() and not write:
            yield None
            return
        if write:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            self.path if write else self.path.as_uri() + "?mode=ro",
            uri=not write,
            check_same_thread=False,
        )
        try:
            if (
                not write
                and not connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='checkpoints'"
                ).fetchone()
            ):
                yield None
                return
            # 官方 setup 会在需要时执行；mode=ro 确保读取不能修改数据库。
            yield SqliteSaver(connection, serde=self.serde)
        finally:
            connection.close()

    def get_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        """读取指定检查点；数据库或检查点不存在时返回 None。"""
        with self._saver(write=False) as saver:
            return saver.get_tuple(config) if saver is not None else None

    def list(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> Iterator[CheckpointTuple]:
        """列出符合会话、元数据和时间条件的历史检查点。"""
        with self._saver(write=False) as saver:
            if saver is not None:
                yield from saver.list(config, filter=filter, before=before, limit=limit)

    def put(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        """保存完整检查点，必要时由官方组件创建或升级会话表。"""
        with self._saver(write=True) as saver:
            if saver is None:
                raise ToolError(_UNINITIALIZED_SESSION, code="storage_failed")
            return saver.put(config, checkpoint, metadata, new_versions)

    def put_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        """保存节点的中间结果，让恢复流程识别已经成功执行的节点。"""
        with self._saver(write=True) as saver:
            if saver is None:
                raise ToolError(_UNINITIALIZED_SESSION, code="storage_failed")
            return saver.put_writes(config, writes, task_id, task_path)

    def delete_thread(self, thread_id: str) -> None:
        """删除指定会话的框架状态；调用方负责先检查权限并持有会话锁。"""
        with self._saver(write=True) as saver:
            if saver is None:
                raise ToolError(_UNINITIALIZED_SESSION, code="storage_failed")
            return saver.delete_thread(thread_id)
