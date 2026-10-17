"""定义任务状态栏及单次运行的临时数据；检索正文不写入状态栏。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal, NotRequired, TypedDict

from langchain.agents import AgentState
from pydantic import TypeAdapter

if TYPE_CHECKING:
    from .builder import ContextBuilder


class ResultReference(TypedDict):
    """记录已成功执行的工具及来源位置，不包含工具正文或任意参数。"""

    id: str
    tool: str
    location: str
    version: str


class TaskItem(TypedDict):
    """保存一个任务项；完成状态必须引用成功工具结果或用户确认。"""

    id: str
    text: str
    status: Literal["pending", "in_progress", "completed", "blocked"]
    evidence: list[str]


class TaskFact(TypedDict):
    """区分带有来源的事实和待验证的判断。"""

    text: str
    source: str
    confirmed: bool


class TaskBoard(TypedDict):
    """保存当前任务的紧凑工作状态，不代替对话或长期记忆。"""

    schema_version: Literal[1]
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


class ContextAgentState(AgentState):
    """为官方 AgentState 增加版本化任务状态，不建立第二套运行状态机。"""

    task_board: NotRequired[TaskBoard]


class ContextUsage(TypedDict):
    """记录请求用量和压缩原因，不记录消息正文。"""

    input_tokens: int
    input_budget: int
    output_tokens: int
    counter: str
    system_tokens: int
    history_tokens: int
    tool_schema_tokens: int
    data_tokens: int
    reasons: list[str]
    sources: list[str]
    actual_input_tokens: int | None
    actual_output_tokens: int | None


@dataclass(slots=True)
class ContextRuntime:
    """由应用注入可信任务信息，缓存只在本次运行内复用。"""

    run_id: str = ""
    session_id: str = ""
    task: str = ""
    constraints: tuple[str, ...] = ()
    criteria: tuple[str, ...] = ()
    new_task: bool = False
    builder: ContextBuilder | None = None
    board: TaskBoard | None = None
    usages: list[ContextUsage] = field(default_factory=list)
    summary_calls: int = 0
    summary_reasons: list[str] = field(default_factory=list)
    seen_changes: set[str] = field(default_factory=set)
    seen_results: set[str] = field(default_factory=set)
    invalid_memory_calls: set[str] = field(default_factory=set)
    refreshes: int = 0
    query: str | None = None
    candidates: list[ContextItem] = field(default_factory=list)
    initialized: bool = False


@dataclass(frozen=True, slots=True)
class ContextItem:
    """保留候选内容的身份、版本和过期时间，选择时不拆开单条记录。"""

    id: str
    kind: Literal["memory", "rag"]
    content: str
    source: str
    version: str
    expires_at: datetime | None = None
    protected: bool = False
    request_only: bool = True

    @property
    def key(self) -> str:
        """不同来源或版本分别计入去重身份。"""
        return f"{self.kind}:{self.source}:{self.id}@{self.version}"


_BOARD = TypeAdapter(TaskBoard)


def restore_board(value: TaskBoard | None) -> TaskBoard | None:
    """迁移旧检查点：没有状态栏的旧格式按空状态处理，未知版本拒绝读取。"""
    if value is None:
        return None
    return _BOARD.validate_python(value)


def utc_now() -> str:
    """返回状态栏使用的 UTC 更新时间。"""
    return datetime.now(UTC).isoformat()
