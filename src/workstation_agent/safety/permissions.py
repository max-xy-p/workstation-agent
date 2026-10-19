"""定义工具权限等级和审批规则。"""

from __future__ import annotations

from enum import IntEnum
from typing import Any, Protocol


class PermissionLevel(IntEnum):
    """按风险划分工具所需的权限等级。"""

    SAFE_READ = 10
    CONTROLLED_WRITE = 20
    SENSITIVE = 30
    DESTRUCTIVE = 40


class PermissionRequired(RuntimeError):
    """表示工具需要审批，当前操作停止。"""


class ApprovalPolicy(Protocol):
    """约定工具调用的授权检查方法。"""

    def allows(
        self, tool_name: str, permission: PermissionLevel, arguments: dict[str, Any]
    ) -> bool: ...


class AllowSafeReadsPolicy(ApprovalPolicy):
    """默认只允许安全读取工具。"""

    def allows(
        self, tool_name: str, permission: PermissionLevel, arguments: dict[str, Any]
    ) -> bool:
        return permission <= PermissionLevel.SAFE_READ
