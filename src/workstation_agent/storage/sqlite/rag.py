"""保存原生框架文档快照；旧格式只识别来源，不解码或转换。"""

import sqlite3
from importlib.resources import files
from pathlib import Path

from ...rag.index import IndexSnapshot, IndexState, validate_snapshot
from ...tools.contracts import ToolError


class SQLiteIndexRepository:
    """按版本化格式原子发布来源、框架 Document 与按 ID 关联的向量。"""

    format_version = 3

    def __init__(
        self, database_path: Path, *, workspace: Path | None = None, max_chunks: int = 10_000
    ):
        self.max_chunks = max_chunks
        self.database_path = database_path.absolute()
        self.workspace = (workspace or database_path.parent).resolve()

    def _check_path(self):
        """每次打开前检查真实路径，拒绝被替换到工作区外的链接。"""
        if not self.database_path.resolve().is_relative_to(self.workspace):
            raise ValueError("RAG 数据路径越界")

    def _write_connection(self):
        """迁移只发生在已授权写入时，并由写事务串行应用。"""
        self._check_path()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.database_path, timeout=10)
        try:
            connection.executescript(
                files("workstation_agent.storage.migrations")
                .joinpath("002_rag.sql")
                .read_text(encoding="utf-8")
            )
            connection.execute("BEGIN IMMEDIATE")
            if not connection.execute("SELECT 1 FROM rag_migrations WHERE version=3").fetchone():
                migration = (
                    files("workstation_agent.storage.migrations")
                    .joinpath("003_rag_native_documents.sql")
                    .read_text(encoding="utf-8")
                )
                # 当前迁移仅含 SQL 语句；逐条执行以保留外层事务的并发锁。
                for statement in migration.split(";"):
                    if statement.strip():
                        connection.execute(statement)
            connection.commit()
        except BaseException:
            connection.rollback()
            connection.close()
            raise
        return connection

    def state(self) -> IndexState:
        """单次 SELECT 读取有效快照，旧格式返回来源列表而不解析 payload。"""
        self._check_path()
        if not self.database_path.exists():
            return IndexState()
        connection = sqlite3.connect(self.database_path.as_uri() + "?mode=ro", uri=True, timeout=10)
        try:
            connection.execute("BEGIN")
            columns = {row[1] for row in connection.execute("PRAGMA table_info(rag_documents)")}
            if not columns:
                return IndexState()
            if "format_version" not in columns:
                return IndexState(
                    obsolete_sources=tuple(
                        row[0]
                        for row in connection.execute(
                            "SELECT source_uri FROM rag_documents ORDER BY source_uri"
                        )
                    )
                )
            rows = connection.execute(
                "SELECT source_uri, bundle, format_version FROM rag_documents ORDER BY source_uri"
            ).fetchall()
            return IndexState(
                snapshots=tuple(
                    IndexSnapshot.model_validate_json(payload)
                    for _, payload, version in rows
                    if version == self.format_version
                ),
                obsolete_sources=tuple(
                    source for source, _, version in rows if version != self.format_version
                ),
            )
        finally:
            connection.close()

    def get(self, source_uri: str) -> IndexSnapshot | None:
        """只返回当前格式的快照，旧来源需要从原文件重建。"""
        return next(
            (item for item in self.state().snapshots if item.source.source_uri == source_uri), None
        )

    def publish(self, snapshot: IndexSnapshot) -> None:
        """验证完整快照后事务替换，失败时保留原记录。"""
        validate_snapshot(snapshot)
        payload = snapshot.model_dump_json()
        connection = self._write_connection()
        try:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                total = connection.execute(
                    "SELECT COALESCE(SUM(json_array_length(bundle, '$.chunks')), 0) "
                    "FROM rag_documents WHERE source_uri != ? AND format_version = ?",
                    (snapshot.source.source_uri, self.format_version),
                ).fetchone()[0]
                if total + len(snapshot.chunks) > self.max_chunks:
                    raise ToolError("RAG chunk budget exceeded", code="resource_limit")
                connection.execute(
                    "INSERT INTO rag_documents(source_uri, document_id, bundle, format_version) "
                    "VALUES (?, ?, ?, ?) ON CONFLICT(source_uri) DO UPDATE SET "
                    "document_id=excluded.document_id, bundle=excluded.bundle, "
                    "format_version=excluded.format_version",
                    (snapshot.source.source_uri, snapshot.source.id, payload, self.format_version),
                )
        finally:
            connection.close()

    def delete(self, source_uri: str) -> bool:
        """授权后删除任意格式的来源记录；不存在数据库时不写入。"""
        self._check_path()
        if not self.database_path.exists():
            return False
        connection = self._write_connection()
        try:
            with connection:
                return bool(
                    connection.execute(
                        "DELETE FROM rag_documents WHERE source_uri=?", (source_uri,)
                    ).rowcount
                )
        finally:
            connection.close()
