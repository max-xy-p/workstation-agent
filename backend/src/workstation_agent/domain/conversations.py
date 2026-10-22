"""定义供用户管理的会话与文本历史，不包含框架检查点或工具原文。"""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal


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
