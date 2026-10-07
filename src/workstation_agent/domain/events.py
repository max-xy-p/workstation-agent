"""定义任务执行过程中的事件通知。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol


class EventType(StrEnum):
    """定义任务、模型和工具事件的类别。"""
    RUN_STARTED = "run.started"
    RUN_STATUS_CHANGED = "run.status_changed"
    MODEL_STARTED = "model.started"
    MODEL_COMPLETED = "model.completed"
    TOOL_STARTED = "tool.started"
    TOOL_COMPLETED = "tool.completed"
    TOOL_DENIED = "tool.denied"
    RUN_COMPLETED = "run.completed"
    RUN_FAILED = "run.failed"


@dataclass(frozen=True, slots=True)
class AgentEvent:
    """记录事件类别、所属任务和附加数据。"""
    # 事件类别。
    type: EventType
    # 所属任务标识。
    run_id: str
    # 关联步骤标识，可为空。
    step_id: str | None = None
    # 附加事件数据，调用方须预先脱敏。
    data: dict[str, Any] = field(default_factory=dict)
    # 事件记录创建时间，使用 UTC。
    occurred_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class EventSink(Protocol):
    """约定事件接收方法。"""
    def emit(self, event: AgentEvent) -> None: ...


class NullEventSink:
    """忽略事件的默认接收器。"""
    def emit(self, event: AgentEvent) -> None:
        pass
