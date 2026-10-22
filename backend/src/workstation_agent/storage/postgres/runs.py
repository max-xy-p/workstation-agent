"""通过 PostgreSQL 保存业务运行和步骤摘要，不承担检查点恢复。"""

from copy import deepcopy

from pydantic import TypeAdapter

from ...domain.runs import Run
from ...memory.service import redact
from .database import PostgresDatabase

_RUN = TypeAdapter(Run)


class PostgresRunRepository:
    """按工作区保存 Run 与所属步骤，事务失败时保留原记录。"""

    def __init__(self, database: PostgresDatabase, *, secrets: tuple[str | None, ...] = ()) -> None:
        self.database: PostgresDatabase = database
        self.secrets = secrets

    def initialize(self) -> None:
        """执行 PostgreSQL 业务表的版本化迁移。"""
        self.database.initialize()

    def save(self, run: Run) -> None:
        """以一条结构化记录保存领域 Run 和步骤，保持结果更新完整。"""
        run = deepcopy(run)
        run.task = redact(run.task, self.secrets)
        run.answer = redact(run.answer, self.secrets) if run.answer is not None else None
        run.error = redact(run.error, self.secrets) if run.error is not None else None
        for step in run.steps:
            step.input_summary = redact(step.input_summary, self.secrets)
            step.output_summary = redact(step.output_summary, self.secrets)
            step.error = redact(step.error, self.secrets) if step.error is not None else None
        with self.database.transaction(write=True) as connection:
            assert connection is not None
            connection.execute(
                "INSERT INTO workstation.runs(workspace_id,id,session_id,status,payload,updated_at) "
                "VALUES (%s,%s,%s,%s,%s::jsonb,%s) ON CONFLICT(workspace_id,id) DO UPDATE SET "
                "status=excluded.status,payload=excluded.payload,updated_at=excluded.updated_at",
                (self.database.workspace_id, run.id, run.session_id, run.status.value,
                 _RUN.dump_json(run).decode(), run.updated_at),
            )

    def get(self, run_id: str) -> Run | None:
        """只读取当前工作区的记录，缺少表或记录时返回 None。"""
        with self.database.transaction() as connection:
            if connection is None:
                return None
            row = connection.execute(
                "SELECT payload::text FROM workstation.runs WHERE workspace_id=%s AND id=%s",
                (self.database.workspace_id, run_id),
            ).fetchone()
            return _RUN.validate_json(row[0]) if row else None
