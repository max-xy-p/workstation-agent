"""定义任务、会话和执行步骤。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from uuid import uuid4


def new_id(prefix: str) -> str:
    """生成带前缀的唯一标识。"""
    return f"{prefix}_{uuid4().hex}"


class RunStatus(StrEnum):
    """定义任务执行状态。"""
    CREATED = "created"
    PREPARING = "preparing"
    THINKING = "thinking"
    ACTING = "acting"
    OBSERVING = "observing"
    WAITING_APPROVAL = "waiting_approval"
    WAITING_USER = "waiting_user"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    LIMIT_REACHED = "limit_reached"


TERMINAL_RUN_STATUSES = {
    RunStatus.COMPLETED,
    RunStatus.FAILED,
    RunStatus.CANCELLED,
    RunStatus.LIMIT_REACHED,
}


class StepKind(StrEnum):
    """定义执行步骤的类别。"""
    MODEL = "model"
    TOOL = "tool"
    MEMORY = "memory"
    RETRIEVAL = "retrieval"


class StepStatus(StrEnum):
    """定义步骤执行状态。"""
    STARTED = "started"
    COMPLETED = "completed"
    FAILED = "failed"
    DENIED = "denied"


@dataclass(slots=True)
class Step:
    """记录一次模型、工具、记忆或检索操作。"""
    # 所属任务标识。
    run_id: str
    # 操作类别。
    kind: StepKind
    # 操作名称。
    name: str
    # 步骤的唯一标识。
    id: str = field(default_factory=lambda: new_id("step"))
    # 步骤执行状态。
    status: StepStatus = StepStatus.STARTED
    # 脱敏输入摘要，默认入口留空。
    input_summary: str = ""
    # 脱敏输出摘要，默认入口留空。
    output_summary: str = ""
    # 可公开的失败说明。
    error: str | None = None
    # 步骤记录创建时间，使用 UTC；不代表实际请求开始时间。
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    # 步骤标记结束的 UTC 时间，不用于统计实际调用耗时。
    finished_at: datetime | None = None

    def finish(self, output: str = "") -> None:
        """记录步骤成功、输出摘要和结束时间。"""
        self.status = StepStatus.COMPLETED
        self.output_summary = output
        self.finished_at = datetime.now(UTC)

    def fail(self, error: str, *, denied: bool = False) -> None:
        """记录步骤失败或拒绝及其说明。"""
        self.status = StepStatus.DENIED if denied else StepStatus.FAILED
        self.error = error
        self.finished_at = datetime.now(UTC)


@dataclass(slots=True)
class Run:
    """保存单次任务、执行步骤和最终结果。"""
    # 用户提交的任务内容。
    task: str
    # 关联会话标识，当前每次任务重新生成。
    session_id: str
    # 任务的唯一标识。
    id: str = field(default_factory=lambda: new_id("run"))
    # 任务执行状态。
    status: RunStatus = RunStatus.CREATED
    # 按观察顺序记录的执行步骤。
    steps: list[Step] = field(default_factory=list)
    # 最终模型答复，完成前为 None。
    answer: str | None = None
    # 可公开的失败或等待说明。
    error: str | None = None
    # 任务创建时间，使用 UTC。
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    # 最近一次状态转换时间，使用 UTC。
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def transition(self, status: RunStatus) -> None:
        """更新任务状态，已结束的任务禁止再次转换。"""
        if self.status in TERMINAL_RUN_STATUSES:
            raise ValueError(f"cannot transition terminal run from {self.status} to {status}")
        self.status = status
        self.updated_at = datetime.now(UTC)

    def complete(self, answer: str) -> None:
        """保存最终答复并标记任务完成。"""
        self.answer = answer
        self.transition(RunStatus.COMPLETED)

    def fail(self, error: str, *, limit_reached: bool = False) -> None:
        """记录任务失败或达到调用上限。"""
        self.error = error
        self.transition(RunStatus.LIMIT_REACHED if limit_reached else RunStatus.FAILED)


@dataclass(slots=True)
class Session:
    """保存会话标识和标题，供任务关联。"""
    # 会话标识，当前入口不复用会话。
    id: str = field(default_factory=lambda: new_id("session"))
    # 可选会话标题。
    title: str | None = None
    # 会话创建时间，使用 UTC。
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
