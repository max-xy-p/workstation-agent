"""定义会话、文本历史及答复引用，不保存框架检查点或完整工具记录。"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from .sources import AnswerSource


@dataclass(frozen=True, slots=True)
class Conversation:
    """保存会话标题、更新时间及用于拒绝过期修改的版本号。"""

    id: str
    title: str
    revision: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ConversationMessage:
    """保存用户文本或最终答复；位置只用于排序，不是工具调用序号。"""

    id: str
    session_id: str
    position: int
    role: Literal["user", "assistant"]
    content: str
    run_id: str | None
    created_at: datetime
    updated_at: datetime
    sources: list[AnswerSource] = field(default_factory=list)
