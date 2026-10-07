"""使用 SQLite 保存和读取任务记录。"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from importlib.resources import files
from pathlib import Path

from ...domain.runs import Run, RunStatus, Step, StepKind, StepStatus


class SQLiteRunRepository:
    """将任务和步骤记录存入 SQLite 数据库。"""
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path

    def initialize(self) -> None:
        """创建数据库目录，并加载初始表结构。"""
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        migration = (
            files("workstation_agent.storage.migrations")
            .joinpath("001_initial.sql")
            .read_text(encoding="utf-8")
        )
        with self._connect() as connection:
            connection.executescript(migration)

    def save(self, run: Run) -> None:
        """新增或更新任务及其步骤记录。"""
        self.initialize()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO runs(id, session_id, task, status, answer, error, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    status=excluded.status,
                    answer=excluded.answer,
                    error=excluded.error,
                    updated_at=excluded.updated_at
                """,
                (
                    run.id,
                    run.session_id,
                    run.task,
                    run.status.value,
                    run.answer,
                    run.error,
                    run.created_at.isoformat(),
                    run.updated_at.isoformat(),
                ),
            )
            for step in run.steps:
                connection.execute(
                    """
                    INSERT INTO steps(
                        id, run_id, kind, name, status, input_summary, output_summary,
                        error, started_at, finished_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        status=excluded.status,
                        output_summary=excluded.output_summary,
                        error=excluded.error,
                        finished_at=excluded.finished_at
                    """,
                    (
                        step.id,
                        step.run_id,
                        step.kind.value,
                        step.name,
                        step.status.value,
                        step.input_summary,
                        step.output_summary,
                        step.error,
                        step.started_at.isoformat(),
                        step.finished_at.isoformat() if step.finished_at else None,
                    ),
                )

    def get(self, run_id: str) -> Run | None:
        """读取任务及其步骤，不存在时返回 None；不恢复图执行。"""
        if not self.database_path.exists():
            return None
        with self._connect() as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            if row is None:
                return None
            step_rows = connection.execute(
                "SELECT * FROM steps WHERE run_id = ? ORDER BY started_at", (run_id,)
            ).fetchall()
        run = Run(
            id=row["id"],
            session_id=row["session_id"],
            task=row["task"],
            status=RunStatus(row["status"]),
            answer=row["answer"],
            error=row["error"],
            created_at=self._parse_datetime(row["created_at"]),
            updated_at=self._parse_datetime(row["updated_at"]),
        )
        run.steps.extend(
            Step(
                id=step_row["id"],
                run_id=step_row["run_id"],
                kind=StepKind(step_row["kind"]),
                name=step_row["name"],
                status=StepStatus(step_row["status"]),
                input_summary=step_row["input_summary"],
                output_summary=step_row["output_summary"],
                error=step_row["error"],
                started_at=self._parse_datetime(step_row["started_at"]),
                finished_at=(
                    self._parse_datetime(step_row["finished_at"])
                    if step_row["finished_at"]
                    else None
                ),
            )
            for step_row in step_rows
        )
        return run

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """管理数据库连接，正常退出时提交并关闭连接。"""
        connection = sqlite3.connect(self.database_path)
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    @staticmethod
    def _parse_datetime(value: str):
        """将数据库中的时间文本转换为时间对象。"""
        from datetime import datetime

        return datetime.fromisoformat(value)
