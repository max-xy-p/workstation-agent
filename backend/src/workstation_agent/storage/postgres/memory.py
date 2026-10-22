"""在 PostgreSQL 事务内保存文本记忆，并检查版本、范围与提取批次。"""

from __future__ import annotations

import json

from psycopg import Connection
from pydantic import JsonValue, TypeAdapter

from ...memory.models import MemoryRecord, MemoryScope
from ...tools.contracts import ToolError
from .database import PostgresDatabase

_METADATA = TypeAdapter(dict[str, JsonValue])


class PostgresMemoryRepository:
    """保持记忆协议，通过工作区分区防止用户范围记忆意外跨工作区传播。"""

    def __init__(self, database: PostgresDatabase, *, max_records: int = 2000) -> None:
        self.database: PostgresDatabase = database
        self.max_records = max_records

    def records(self, scopes: tuple[MemoryScope, ...]) -> tuple[MemoryRecord, ...]:
        """读取当前工作区及允许范围内的正文，不执行表初始化。"""
        if not scopes:
            return ()
        with self.database.transaction() as connection:
            if connection is None:
                return ()
            rows = connection.execute(
                "SELECT content,payload::text FROM workstation.memory_records "
                "WHERE workspace_id=%s AND namespace=ANY(%s) ORDER BY id",
                (self.database.workspace_id, [scope.namespace for scope in scopes]),
            ).fetchall()
            return tuple(MemoryRecord.model_validate_json(json.dumps({
                **_METADATA.validate_json(payload), "content": content,
            })) for content, payload in rows)

    def generation(self, scope: MemoryScope) -> int:
        """读取遗忘版本，未创建范围时返回零。"""
        with self.database.transaction() as connection:
            if connection is None:
                return 0
            row = connection.execute(
                "SELECT generation FROM workstation.memory_scopes WHERE workspace_id=%s AND namespace=%s",
                (self.database.workspace_id, scope.namespace),
            ).fetchone()
            return int(row[0]) if row else 0

    def processed(self, scope: MemoryScope, batch_id: str) -> bool:
        """检查提取批次，遗忘记忆不会删除已处理标记。"""
        with self.database.transaction() as connection:
            return connection is not None and connection.execute(
                "SELECT 1 FROM workstation.memory_batches WHERE workspace_id=%s AND namespace=%s AND batch_id=%s",
                (self.database.workspace_id, scope.namespace, batch_id),
            ).fetchone() is not None

    def apply(
        self, records: tuple[MemoryRecord, ...], expected: dict[str, int | None], scope: MemoryScope,
        *, generation: int | None = None, batch_id: str | None = None,
    ) -> bool:
        """工作区内串行检查版本和容量，任一记录失败时整批回滚。"""
        namespace = scope.namespace
        with self.database.transaction(write=True) as connection:
            assert connection is not None
            # 记录 ID 跨范围唯一，统一工作区锁还要保护并发的跨范围 ID 冲突。
            self.database.lock(connection, "memory-write")
            if batch_id and connection.execute(
                "SELECT 1 FROM workstation.memory_batches WHERE workspace_id=%s AND namespace=%s AND batch_id=%s",
                (self.database.workspace_id, namespace, batch_id),
            ).fetchone():
                return False
            row = connection.execute(
                "SELECT generation FROM workstation.memory_scopes WHERE workspace_id=%s AND namespace=%s",
                (self.database.workspace_id, namespace),
            ).fetchone()
            if generation is not None and generation != (int(row[0]) if row else 0):
                raise ToolError("提取期间发生遗忘，请放弃旧候选", code="stale_extraction")
            for record in records:
                self._store(connection, namespace, record, expected.get(record.id))
            if batch_id:
                connection.execute("INSERT INTO workstation.memory_batches VALUES (%s,%s,%s)",
                                   (self.database.workspace_id, namespace, batch_id))
        return True

    def _store(self, connection: Connection, namespace: str, record: MemoryRecord, version: int | None) -> None:
        """拒绝归属或版本冲突，正文只保存在 content 列。"""
        row = connection.execute(
            "SELECT namespace,version FROM workstation.memory_records WHERE workspace_id=%s AND id=%s",
            (self.database.workspace_id, record.id),
        ).fetchone()
        if (row is None and version is not None) or (row and (row[0] != namespace or row[1] != version)):
            raise ToolError("记忆版本已改变，请重新读取", code="version_conflict")
        if version is None:
            count_row = connection.execute(
                "SELECT COUNT(*) FROM workstation.memory_records WHERE workspace_id=%s AND namespace=%s",
                (self.database.workspace_id, namespace),
            ).fetchone()
            if count_row and count_row[0] >= self.max_records:
                raise ToolError("记忆条数超过范围预算", code="resource_limit")
        connection.execute(
            "INSERT INTO workstation.memory_records VALUES (%s,%s,%s,%s,%s::jsonb,%s) "
            "ON CONFLICT(workspace_id,id) DO UPDATE SET content=excluded.content,payload=excluded.payload,version=excluded.version",
            (self.database.workspace_id, record.id, namespace, record.content,
             record.model_dump_json(exclude={"content"}), record.version),
        )

    def delete(self, memory_id: str, scope: MemoryScope, version: int) -> bool:
        """按版本删除正文并增加遗忘版本，与批次保存使用相同范围锁。"""
        with self.database.transaction(write=True) as connection:
            assert connection is not None
            self.database.lock(connection, "memory-write")
            deleted = connection.execute(
                "DELETE FROM workstation.memory_records WHERE workspace_id=%s AND id=%s AND namespace=%s AND version=%s",
                (self.database.workspace_id, memory_id, scope.namespace, version),
            ).rowcount
            if deleted != 1:
                raise ToolError("记忆版本已改变，请重新读取", code="version_conflict")
            connection.execute(
                "INSERT INTO workstation.memory_scopes VALUES (%s,%s,1) "
                "ON CONFLICT(workspace_id,namespace) DO UPDATE SET generation=workstation.memory_scopes.generation+1",
                (self.database.workspace_id, scope.namespace),
            )
        return True
