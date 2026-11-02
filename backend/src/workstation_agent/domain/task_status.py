"""定义可持久化的任务目标、进度和结果引用，不依赖执行框架。"""

from typing import Literal, TypedDict


class ResultReference(TypedDict):
    """记录已成功执行的工具及来源位置，不包含工具正文或任意参数。"""

    id: str
    tool: str
    location: str
    version: str


class TaskItem(TypedDict):
    """保存一个任务项；模型只能报告待确认，完成状态由用户入口验收。"""

    id: str
    text: str
    status: Literal["pending", "in_progress", "pending_confirmation", "completed", "blocked"]
    evidence: list[str]


class TaskFact(TypedDict):
    """区分带有来源的事实和待验证的判断。"""

    text: str
    source: str
    confirmed: bool


class _TaskBoardFields(TypedDict):
    """保存当前任务的紧凑工作状态，不代替对话或长期记忆。"""

    task_id: str
    session_id: str
    updated_at: str
    goal: str
    current_request: str
    criteria: list[str]
    constraints: list[str]
    items: list[TaskItem]
    facts: list[TaskFact]
    results: list[ResultReference]
    blockers: list[str]
    next_action: str


class TaskBoard(_TaskBoardFields):
    """格式 2 增加明确的验收版本，只有用户入口才能确认任务完成。"""

    schema_version: Literal[2]
    revision: int


class LegacyTaskBoard(_TaskBoardFields):
    """读取格式 1 的旧检查点，迁移后不保留旧的自动完成判断。"""

    schema_version: Literal[1]


