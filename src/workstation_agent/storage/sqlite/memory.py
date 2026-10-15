"""SQLite 长期记忆适配；正文唯一保存于 TEXT 列，读操作不创建文件。"""

import json
import sqlite3
from contextlib import contextmanager
from importlib.resources import files

from ...memory.models import MemoryRecord
from ...tools.contracts import ToolError


class SQLiteMemoryRepository:
    """事务校验版本和删除水位，派生检索按有效正文重建。"""

    def __init__(self, database_path, workspace, *, max_records=2000):
        self.database_path = database_path.absolute()
        self.workspace = workspace.resolve()
        self.max_records = max_records

    @contextmanager
    def _connect(self, write=False):
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

    def records(self, scopes):
        """单次快照读取允许范围，正文不重复放入 JSON 元数据。"""
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

    def generation(self, scope):
        """读出防重建水位；空数据库的水位为零。"""
        with self._connect() as connection:
            if connection is None:
                return 0
            row = connection.execute(
                "SELECT generation FROM memory_scopes WHERE namespace=?", (scope.namespace,)
            ).fetchone()
            return row[0] if row else 0

    def processed(self, scope, batch_id):
        """已处理来源在遗忘后仍保留，避免旧批次重新生成正文。"""
        with self._connect() as connection:
            return (
                connection is not None
                and connection.execute(
                    "SELECT 1 FROM memory_batches WHERE namespace=? AND batch_id=?",
                    (scope.namespace, batch_id),
                ).fetchone()
                is not None
            )

    def apply(self, records, expected, scope, *, generation=None, batch_id=None):
        """完整批次原子提交，已处理批次幂等，删除后旧候选不生效。"""
        namespace = scope.namespace
        with self._connect(True) as connection:
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
            if generation is not None and generation != (row[0] if row else 0):
                raise ToolError("提取期间发生遗忘，请放弃旧候选", code="stale_extraction")
            for record in records:
                row = connection.execute(
                    "SELECT namespace,version FROM memory_records WHERE id=?", (record.id,)
                ).fetchone()
                version = expected.get(record.id)
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
                    "INSERT INTO memory_records(id,namespace,content,payload,version) "
                    "VALUES(?,?,?,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET content=excluded.content,"
                    "payload=excluded.payload,"
                    "version=excluded.version",
                    (
                        record.id,
                        namespace,
                        record.content,
                        record.model_dump_json(exclude={"content"}),
                        record.version,
                    ),
                )
            if batch_id:
                connection.execute("INSERT INTO memory_batches VALUES(?,?)", (namespace, batch_id))
        return True

    def delete(self, memory_id, scope, version):
        """删除正文且保留无内容的范围水位，错误版本不产生副作用。"""
        with self._connect(True) as connection:
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
