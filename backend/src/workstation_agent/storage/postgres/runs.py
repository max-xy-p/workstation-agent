"""通过 PostgreSQL 保存业务运行和步骤摘要，不承担检查点恢复。"""

from copy import deepcopy

from pydantic import JsonValue, TypeAdapter

from ...api.conversations import Page, PageRequest
from ...domain.runs import Run
from ...memory.service import redact
from ...tools.contracts import ToolError
from .database import PostgresDatabase

_RUN = TypeAdapter(Run)


def _clean(value: JsonValue, secrets: tuple[str | None, ...]) -> JsonValue:
    """只脱敏 JSON 字符串值，避免替换原始 JSON 时破坏引号和字段结构。"""
    if isinstance(value, str):
        return redact(value, secrets)
    if isinstance(value, list):
        return [_clean(item, secrets) for item in value]
    if isinstance(value, dict):
        return {key: _clean(item, secrets) for key, item in value.items()}
    return value


class PostgresRunRepository:
    """按工作区保存 Run 与所属步骤，事务失败时保留原记录。"""

    def __init__(self, database: PostgresDatabase, *, scope: str = "", secrets: tuple[str | None, ...] = ()) -> None:
        self.database: PostgresDatabase = database
        self.scope = scope
        self.secrets = secrets

    def initialize(self) -> None:
        """执行 PostgreSQL 业务表的版本化迁移。"""
        self.database.initialize()

    def save(self, run: Run) -> None:
        """以一条结构化记录保存领域 Run 和步骤，保持结果更新完整。"""
        # 先序列化整个领域对象再脱敏，任务状态栏中的文本也必须经过相同检查。
        payload = TypeAdapter(JsonValue).dump_json(_clean(
            TypeAdapter(JsonValue).validate_json(_RUN.dump_json(deepcopy(run))), self.secrets,
        )).decode()
        with self.database.transaction(write=True) as connection:
            assert connection is not None
            connection.execute(
                "INSERT INTO workstation.runs(workspace_id,scope,id,session_id,status,payload,updated_at) "
                "VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s) ON CONFLICT(workspace_id,scope,id) DO UPDATE SET "
                "status=excluded.status,payload=excluded.payload,updated_at=excluded.updated_at",
                (self.database.workspace_id, self.scope, run.id, run.session_id, run.status.value,
                 payload, run.updated_at),
            )

    def get(self, run_id: str) -> Run | None:
        """只读取当前工作区的记录，缺少表或记录时返回 None。"""
        with self.database.transaction() as connection:
            if connection is None:
                return None
            row = connection.execute(
                "SELECT payload::text FROM workstation.runs WHERE workspace_id=%s AND scope=%s AND id=%s",
                (self.database.workspace_id, self.scope, run_id),
            ).fetchone()
            return _RUN.validate_json(row[0]) if row else None

    def list_runs(self, page: PageRequest, *, session_id: str | None = None) -> Page[Run]:
        """按更新时间和 ID 倒序分页，只返回当前身份与工作区的运行。"""
        with self.database.transaction() as connection:
            if connection is None:
                return Page(items=[], next_offset=None)
            rows = connection.execute(
                "SELECT payload::text FROM workstation.runs WHERE workspace_id=%s AND scope=%s "
                "AND (%s::text IS NULL OR session_id=%s) ORDER BY updated_at DESC,id DESC LIMIT %s OFFSET %s",
                (self.database.workspace_id, self.scope, session_id, session_id, page.limit + 1, page.offset),
            ).fetchall()
            return Page(items=[_RUN.validate_json(row[0]) for row in rows[:page.limit]],
                        next_offset=page.offset + page.limit if len(rows) > page.limit else None)

    def unfinished(self) -> list[Run]:
        """只读取需核对检查点的未结束记录，不扫描已结束运行的步骤。"""
        with self.database.transaction() as connection:
            if connection is None:
                return []
            rows = connection.execute(
                "SELECT payload::text FROM workstation.runs WHERE workspace_id=%s AND scope=%s "
                "AND status NOT IN ('completed','failed','cancelled','limit_reached','interrupted')",
                (self.database.workspace_id, self.scope),
            ).fetchall()
            return [_RUN.validate_json(row[0]) for row in rows]

    def delete(self, run_id: str) -> None:
        """删除已结束运行；数据库再次检查状态，防止误删活动记录。"""
        with self.database.transaction(write=True) as connection:
            assert connection is not None
            row = connection.execute(
                "SELECT status FROM workstation.runs WHERE workspace_id=%s AND scope=%s AND id=%s FOR UPDATE",
                (self.database.workspace_id, self.scope, run_id),
            ).fetchone()
            if row is None:
                raise ToolError("运行不存在", code="not_found")
            if row[0] not in {'completed', 'failed', 'cancelled', 'limit_reached', 'interrupted'}:
                raise ToolError("请先结束运行或处理审批", code="session_busy")
            connection.execute(
                "DELETE FROM workstation.runs WHERE workspace_id=%s AND scope=%s AND id=%s",
                (self.database.workspace_id, self.scope, run_id),
            )
