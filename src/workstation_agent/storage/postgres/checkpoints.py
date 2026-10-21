"""以官方 PostgresSaver 保存检查点，用数据库会话锁保护整次运行。"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from threading import RLock
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
)
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.checkpoint.serde.base import SerializerProtocol
from psycopg.rows import tuple_row

from ...tools.contracts import ToolError
from ..checkpoints import SessionLease
from ..session_serializer import SessionSerializer
from .database import PostgresDatabase


class PostgresSessionBackend:
    """实现现有同步检查点协议，异步调用继续由权限包装器在线程内完成。"""

    def __init__(
        self, database: PostgresDatabase, *, secrets: tuple[str | None, ...] = ()
    ) -> None:
        self.database: PostgresDatabase = database
        self.serde: SerializerProtocol = SessionSerializer(secrets)
        self._initialization_lock = RLock()
        self._initialized = False

    def acquire(self, thread_id: str) -> SessionLease:
        """非阻塞获取数据库会话锁，连接关闭时由 PostgreSQL 自动释放。"""
        connection = self.database.open_connection(tuple_row, autocommit=True)
        try:
            row = connection.execute(
                "SELECT pg_try_advisory_lock(%s)",
                (self.database.lock_key("session:" + thread_id, global_lock=True),),
            ).fetchone()
            if row is None or not row[0]:
                raise ToolError("会话正在执行，请稍后重试", code="session_busy")
            return connection
        except BaseException:
            connection.close()
            raise

    def initialize(self) -> None:
        """在独立 schema 内运行官方迁移；并发初始化使用全局数据库会话锁。"""
        with self._initialization_lock:
            if self._initialized:
                return
            with self.database.checkpoint_connection() as connection:
                key = self.database.lock_key("checkpoint-migrations", global_lock=True)
                connection.execute("SELECT pg_advisory_lock(%s)", (key,))
                try:
                    connection.execute("CREATE SCHEMA IF NOT EXISTS workstation_checkpoints")
                    connection.execute("SET search_path TO workstation_checkpoints")
                    PostgresSaver(connection, serde=self.serde).setup()
                finally:
                    # 池连接归还后仍保持打开，因此必须显式释放迁移锁。
                    connection.execute("SELECT pg_advisory_unlock(%s)", (key,))
            self._initialized = True

    @contextmanager
    def _saver(self, *, write: bool) -> Iterator[PostgresSaver | None]:
        if write:
            self.initialize()
        with self.database.checkpoint_connection() as connection:
            if not write:
                connection.execute("SET default_transaction_read_only TO on")
                row = connection.execute(
                    "SELECT to_regclass('workstation_checkpoints.checkpoints') AS relation"
                ).fetchone()
                if row is None or row["relation"] is None:
                    yield None
                    return
            connection.execute("SET search_path TO workstation_checkpoints")
            yield PostgresSaver(connection, serde=self.serde)

    def get_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        """只读取得官方检查点，未初始化时返回 None。"""
        with self._saver(write=False) as saver:
            return saver.get_tuple(config) if saver is not None else None

    def list(
        self, config: RunnableConfig | None, *, filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None, limit: int | None = None,
    ) -> Iterator[CheckpointTuple]:
        """沿用官方元数据筛选和检查点顺序，不自行解析内部表。"""
        with self._saver(write=False) as saver:
            if saver is not None:
                yield from saver.list(config, filter=filter, before=before, limit=limit)

    def put(
        self, config: RunnableConfig, checkpoint: Checkpoint, metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        """授权后保存完整检查点，必要时执行官方迁移。"""
        with self._saver(write=True) as saver:
            assert saver is not None
            return saver.put(config, checkpoint, metadata, new_versions)

    def put_writes(
        self, config: RunnableConfig, writes: Sequence[tuple[str, Any]], task_id: str, task_path: str = "",
    ) -> None:
        """保存节点中间结果，恢复时避免重放已成功节点。"""
        with self._saver(write=True) as saver:
            assert saver is not None
            saver.put_writes(config, writes, task_id, task_path)

    def delete_thread(self, thread_id: str) -> None:
        """在调用方持有会话锁并通过权限检查后删除框架状态。"""
        with self._saver(write=True) as saver:
            assert saver is not None
            saver.delete_thread(thread_id)
