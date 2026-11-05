"""在 PostgreSQL 中发布原生框架文档快照，保持现有检索与分块协议。"""

from psycopg import Connection

from ...rag.index import IndexSnapshot, IndexState, validate_snapshot
from ...tools.contracts import ToolError
from ...tools.execution import check_execution
from .database import PostgresDatabase


class PostgresIndexRepository:
    """按工作区保存索引，容量检查和快照替换在同一事务内完成。"""

    format_version = 3

    def __init__(self, database: PostgresDatabase, *, max_chunks: int = 10_000) -> None:
        self.database: PostgresDatabase = database
        self.max_chunks = max_chunks

    def state(self) -> IndexState:
        """读取一致的索引列表，旧格式仅返回来源名称，不解析正文。"""
        with self.database.transaction() as connection:
            if connection is None:
                return IndexState()
            rows = connection.execute(
                "SELECT source_uri,bundle::text,format_version FROM workstation.rag_documents "
                "WHERE workspace_id=%s ORDER BY source_uri", (self.database.workspace_id,),
            ).fetchall()
            return IndexState(
                snapshots=tuple(IndexSnapshot.model_validate_json(payload)
                                for _, payload, version in rows if version == self.format_version),
                obsolete_sources=tuple(source for source, _, version in rows if version != self.format_version),
            )

    def get(self, source_uri: str) -> IndexSnapshot | None:
        """只返回当前格式的来源快照。"""
        return next((item for item in self.state().snapshots if item.source.source_uri == source_uri), None)

    def revision(self, source_uri: str) -> int:
        """读取来源版本，迁移前已有的索引从版本零开始。"""
        with self.database.transaction() as connection:
            if connection is None:
                return 0
            table = connection.execute("SELECT to_regclass('workstation.rag_revisions')").fetchone()
            return self._revision(connection, source_uri) if table and table[0] else 0

    def _revision(self, connection: Connection, source_uri: str) -> int:
        row = connection.execute(
            "SELECT revision FROM workstation.rag_revisions WHERE workspace_id=%s AND source_uri=%s",
            (self.database.workspace_id, source_uri),
        ).fetchone()
        return int(row[0]) if row else 0

    def _advance(self, connection: Connection, source_uri: str) -> None:
        connection.execute(
            "INSERT INTO workstation.rag_revisions VALUES (%s,%s,1) "
            "ON CONFLICT(workspace_id,source_uri) DO UPDATE SET revision=workstation.rag_revisions.revision+1",
            (self.database.workspace_id, source_uri),
        )

    def publish(self, snapshot: IndexSnapshot, *, expected_revision: int | None = None) -> None:
        """发布前验证证据和向量，同一工作区并发发布共用预算锁。"""
        validate_snapshot(snapshot)
        with self.database.transaction(write=True) as connection:
            assert connection is not None
            self.database.lock(connection, "rag-budget")
            source_uri = snapshot.source.source_uri
            if expected_revision is not None and self._revision(connection, source_uri) != expected_revision:
                raise ToolError("索引已变化，请重新准备", code="version_conflict")
            row = connection.execute(
                "SELECT COALESCE(SUM(jsonb_array_length(bundle->'chunks')),0) "
                "FROM workstation.rag_documents WHERE workspace_id=%s AND source_uri!=%s AND format_version=%s",
                (self.database.workspace_id, snapshot.source.source_uri, self.format_version),
            ).fetchone()
            if row and int(row[0]) + len(snapshot.chunks) > self.max_chunks:
                raise ToolError("RAG chunk budget exceeded", code="resource_limit")
            check_execution()
            connection.execute(
                "INSERT INTO workstation.rag_documents VALUES (%s,%s,%s,%s::jsonb,%s) "
                "ON CONFLICT(workspace_id,source_uri) DO UPDATE SET document_id=excluded.document_id,"
                "bundle=excluded.bundle,format_version=excluded.format_version",
                (self.database.workspace_id, snapshot.source.source_uri, snapshot.source.id,
                 snapshot.model_dump_json(), self.format_version),
            )
            self._advance(connection, source_uri)

    def delete(self, source_uri: str) -> bool:
        """删除当前工作区的来源，其他工作区的相同路径不受影响。"""
        with self.database.transaction(write=True) as connection:
            assert connection is not None
            self.database.lock(connection, "rag-budget")
            deleted = connection.execute(
                "DELETE FROM workstation.rag_documents WHERE workspace_id=%s AND source_uri=%s",
                (self.database.workspace_id, source_uri),
            ).rowcount == 1
            if deleted:
                self._advance(connection, source_uri)
            return deleted
