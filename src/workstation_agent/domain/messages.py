"""定义上下文组装使用的对话消息。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

Role = Literal["system", "user", "assistant", "tool"]


@dataclass(frozen=True, slots=True)
class Message:
    """保存消息角色、正文和附加信息。"""
    # 消息发送方的角色。
    role: Role
    # 消息正文。
    content: str
    # 发送者或工具名称，默认入口不转发。
    name: str | None = None
    # 关联工具调用的标识，默认入口由框架消息管理。
    tool_call_id: str | None = None
    # 附加消息标签，默认入口不转发。
    metadata: dict[str, Any] = field(default_factory=dict)
