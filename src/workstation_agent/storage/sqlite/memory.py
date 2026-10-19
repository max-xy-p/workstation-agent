"""SQLite 长期记忆适配；正文唯一保存于 TEXT 列，读操作不创建文件。"""

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from importlib.resources import files
from pathlib import Path

from ...memory.models import MemoryRecord, MemoryScope
from ...tools.contracts import ToolError


class SQLiteMemoryRepository:
    """在同一事务中检查记录版本和范围变更编号，避免覆盖新值或写回已遗忘的内容。"""

    def __init__(self, database_path: Path, workspace: Path, *, max_records: int = 2000) -> None:
        self.database_path = database_path.absolute()
        self.workspace = workspace.resolve()
        self.max_records = max_records

    @contextmanager
    def _connect(self, write: bool = False) -> Iterator[sqlite3.Connection | None]:
        if not self.database_path.resolve().is_relative_to(self.workspace):
            raise ToolError("记忆数据路径越界", code="path_outside_workspace")
        if not write and not self.database_path.exists():
            yield None
            return
        if write:
            self.database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            self.database_path if write else self.database_path.as_uri() + "?mode=ro",
            uri=not write,
            timeout=10,
        )
        try:
            if write:
                connection.executescript(
                    files("workstation_agent.storage.migrations")
                    .joinpath("004_memory.sql")
                    .read_text(encoding="utf-8")
                )
                connection.execute("BEGIN IMMEDIATE")
            elif not connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory_records'"
            ).fetchone():
                yield None
                return
            yield connection
            if write:
                connection.commit()
        except BaseException:
            if write:
                connection.rollback()
            raise
        finally:
            connection.close()

    def records(self, scopes: tuple[MemoryScope, ...]) -> tuple[MemoryRecord, ...]:
        """一次读取允许范围内的正文和元数据，读取不会创建数据库。"""
        namespaces = [scope.namespace for scope in scopes]
        with self._connect() as connection:
            if connection is None or not namespaces:
                return ()
            rows = connection.execute(
                "SELECT content,payload FROM memory_records WHERE namespace IN ("
                + ",".join("?" for _ in namespaces)
                + ") ORDER BY id",
                namespaces,
            ).fetchall()
            return tuple(
                MemoryRecord.model_validate_json(
                    json.dumps({**json.loads(payload), "content": content})
                )
                for content, payload in rows
            )

    def generation(self, scope: MemoryScope) -> int:
        """读出范围变更编号；每次删除会增加编号，空数据库返回零。"""
        with self._connect() as connection:
            if connection is None:
                return 0
            row = connection.execute(
                "SELECT generation FROM memory_scopes WHERE namespace=?", (scope.namespace,)
            ).fetchone()
            return row[0] if row else 0

    def processed(self, scope: MemoryScope, batch_id: str) -> bool:
        """检查提取批次是否已处理；处理记录在遗忘后仍保留，避免旧批次重新生成正文。"""
        with self._connect() as connection:
            return (
                connection is not None
                and connection.execute(
                    "SELECT 1 FROM memory_batches WHERE namespace=? AND batch_id=?",
                    (scope.namespace, batch_id),
                ).fetchone()
                is not None
            )

    def apply(
        self,
        records: tuple[MemoryRecord, ...],
        expected: dict[str, int | None],
        scope: MemoryScope,
        *,
        generation: int | None = None,
        batch_id: str | None = None,
    ) -> bool:
        """在同一事务中检查批次、范围版本和记录版本，任一失败则整批撤销。"""
        namespace = scope.namespace
        with self._connect(True) as connection:
            if connection is None:
                raise ToolError("记忆存储未初始化", code="storage_failed")
            if not self._check_batch(connection, namespace, generation, batch_id):
                return False
            for record in records:
                self._store_record(connection, namespace, record, expected.get(record.id))
            if batch_id:
                connection.execute("INSERT INTO memory_batches VALUES(?,?)", (namespace, batch_id))
        return True

    @staticmethod
    def _check_batch(
        connection: sqlite3.Connection, namespace: str, generation: int | None, batch_id: str | None
    ) -> bool:
        """重复批次直接跳过；遗忘前开始的提取不能写回刚删除的内容。"""
        if (
            batch_id
            and connection.execute(
                "SELECT 1 FROM memory_batches WHERE namespace=? AND batch_id=?",
                (namespace, batch_id),
            ).fetchone()
        ):
            return False
        row = connection.execute(
            "SELECT generation FROM memory_scopes WHERE namespace=?", (namespace,)
        ).fetchone()
        current_generation = row[0] if row else 0
        if generation is not None and generation != current_generation:
            raise ToolError("提取期间发生遗忘，请放弃旧候选", code="stale_extraction")
        return True

    def _store_record(
        self,
        connection: sqlite3.Connection,
        namespace: str,
        record: MemoryRecord,
        version: int | None,
    ) -> None:
        """在持有写事务时检查记录归属、版本和容量，再保存正文及元数据。"""
        row = connection.execute(
            "SELECT namespace,version FROM memory_records WHERE id=?", (record.id,)
        ).fetchone()
        if (row is None and version is not None) or (
            row is not None and (row[0] != namespace or row[1] != version)
        ):
            raise ToolError("记忆版本已改变，请重新读取", code="version_conflict")
        if version is None:
            count = connection.execute(
                "SELECT COUNT(*) FROM memory_records WHERE namespace=?", (namespace,)
            ).fetchone()[0]
            if count >= self.max_records:
                raise ToolError("记忆条数超过范围预算", code="resource_limit")
        connection.execute(
            "INSERT INTO memory_records(id,namespace,content,payload,version) VALUES(?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET content=excluded.content,payload=excluded.payload,"
            "version=excluded.version",
            (
                record.id,
                namespace,
                record.content,
                record.model_dump_json(exclude={"content"}),
                record.version,
            ),
        )

    def delete(self, memory_id: str, scope: MemoryScope, version: int) -> bool:
        """按版本删除正文并增加范围变更编号，版本不符时不修改数据库。"""
        with self._connect(True) as connection:
            if connection is None:
                raise ToolError("记忆存储未初始化", code="storage_failed")
            cursor = connection.execute(
                "DELETE FROM memory_records WHERE id=? AND namespace=? AND version=?",
                (memory_id, scope.namespace, version),
            )
            if cursor.rowcount != 1:
                raise ToolError("记忆版本已改变，请重新读取", code="version_conflict")
            connection.execute(
                "INSERT INTO memory_scopes VALUES(?,1) ON CONFLICT(namespace) "
                "DO UPDATE SET generation=generation+1",
                (scope.namespace,),
            )
        return True
