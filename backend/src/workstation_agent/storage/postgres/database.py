"""集中管理 PostgreSQL 连接、业务迁移和事务，不在构造时访问网络。"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from importlib.resources import files
from typing import LiteralString, cast

import psycopg
from psycopg import Connection, sql
from psycopg.rows import DictRow, RowFactory
from psycopg_pool import PoolTimeout

from ...tools.contracts import ToolError
from .pools import pools_for


class PostgresDatabase:
    """为存储实现提供连接和固定工作区分区；凭据不出现在对象表示或错误中。"""

    def __init__(self, url: str | None, workspace_id: str) -> None:
        self._url = url
        self.workspace_id = workspace_id

    @contextmanager
    def connect(self, *, autocommit: bool = False) -> Iterator[Connection]:
        """从官方连接池借用连接，结束时提交或回滚并归还。"""
        if not self._url:
            raise ToolError("请配置 WORKSTATION_AGENT_DATABASE_URL", code="database_not_configured")
        try:
            with pools_for(self._url).records.connection() as connection:
                connection.autocommit = autocommit
                yield connection
        except (psycopg.Error, PoolTimeout):
            raise ToolError("PostgreSQL 操作失败，请检查连接和数据库权限", code="database_failed") from None

    def open_connection[Row](
        self, row_factory: RowFactory[Row], *, autocommit: bool = False
    ) -> Connection[Row]:
        """打开带官方行类型的连接；长期会话锁的调用方必须在结束时关闭它。"""
        if not self._url:
            raise ToolError("请配置 WORKSTATION_AGENT_DATABASE_URL", code="database_not_configured")
        try:
            return Connection[Row].connect(
                self._url, connect_timeout=5, autocommit=autocommit, row_factory=row_factory,
                options="-c statement_timeout=30000 -c lock_timeout=5000",
            )
        except psycopg.Error:
            raise ToolError("PostgreSQL 连接失败，请检查配置", code="database_failed") from None

    @contextmanager
    def checkpoint_connection(self) -> Iterator[Connection[DictRow]]:
        """为官方检查点借用自动提交的字典行连接，归还时清除会话设置。"""
        if not self._url:
            raise ToolError("请配置 WORKSTATION_AGENT_DATABASE_URL", code="database_not_configured")
        try:
            with pools_for(self._url).checkpoints.connection() as connection:
                yield connection
        except (psycopg.Error, PoolTimeout):
            raise ToolError("PostgreSQL 操作失败，请检查连接和数据库权限", code="database_failed") from None

    def initialize(self) -> None:
        """按版本执行随包迁移；并发初始化由数据库事务锁保护。"""
        with self.connect() as connection:
            self.lock(connection, "business-migrations", global_lock=True)
            connection.execute("CREATE SCHEMA IF NOT EXISTS workstation")
            connection.execute("CREATE TABLE IF NOT EXISTS workstation.migrations(version INTEGER PRIMARY KEY)")
            for version, filename in ((5, "005_postgresql.sql"), (6, "006_conversations.sql"), (7, "007_run_history.sql")):
                if connection.execute(
                    "SELECT 1 FROM workstation.migrations WHERE version=%s", (version,)
                ).fetchone():
                    continue
                migration = files("workstation_agent.storage.migrations").joinpath(filename)
                # SQL 仅来自随包发布的迁移资源，不接受用户传入的 SQL 文本。
                migration_sql = cast(LiteralString, migration.read_text(encoding="utf-8"))
                connection.execute(sql.SQL(migration_sql))
                connection.execute("INSERT INTO workstation.migrations(version) VALUES (%s)", (version,))

    @contextmanager
    def transaction(self, *, write: bool = False) -> Iterator[Connection | None]:
        """读取不创建表；写入先迁移，再在独立事务中保存业务变更。"""
        if write:
            self.initialize()
        with self.connect() as connection:
            if not write:
                connection.execute("SET TRANSACTION READ ONLY")
                row = connection.execute("SELECT to_regclass('workstation.migrations')").fetchone()
                if row is None or row[0] is None:
                    yield None
                    return
            yield connection

    def check(self) -> bool:
        """只检查数据库连接，不建表、不执行迁移。"""
        with self.connect() as connection:
            return connection.execute("SELECT 1").fetchone() == (1,)

    def lock_key(self, name: str, *, global_lock: bool = False) -> int:
        """稳定生成锁编号，业务锁包含工作区标识。"""
        scope = "workstation-agent" if global_lock else self.workspace_id
        return int.from_bytes(hashlib.sha256(f"{scope}:{name}".encode()).digest()[:8], signed=True)

    def lock(self, connection: Connection, name: str, *, global_lock: bool = False) -> None:
        """持有事务级锁直到提交或回滚，保护范围预算和整批变更。"""
        key = self.lock_key(name, global_lock=global_lock)
        connection.execute("SELECT pg_advisory_xact_lock(%s)", (key,))
