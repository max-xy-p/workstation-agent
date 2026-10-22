"""定义任务记录的存储接口。"""

from __future__ import annotations

from typing import Protocol

from ..domain.runs import Run


class RunRepository(Protocol):
    """约定任务和步骤记录的保存、读取方法。"""
    def save(self, run: Run) -> None: ...
    def get(self, run_id: str) -> Run | None: ...
