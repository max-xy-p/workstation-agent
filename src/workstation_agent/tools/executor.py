"""在工具真正操作文件等资源前检查参数和权限，并统一处理返回结果。"""

from __future__ import annotations

from typing import Any

from pydantic import ConfigDict
from pydantic.dataclasses import dataclass

from ..safety.permissions import ApprovalPolicy, PermissionRequired
from .contracts import ToolError, ValidatedTool
from .registry import ToolRegistry


@dataclass(frozen=True, config=ConfigDict(strict=True, extra="forbid"))
class ToolResult:
    """保存一次工具调用的内容、成功状态和错误说明。"""

    # 本次调用的工具名称。
    tool_name: str
    # 工具输出或安全的错误文本。
    content: str
    # 本次工具调用是否成功。
    success: bool = True
    # 失败说明，成功时为 None。
    error: str | None = None
    # 供程序判断的错误类别，成功时为 None。
    error_code: str | None = None


class ToolExecutor:
    """检查参数和权限后执行工具，并限制输出及处理错误。"""

    def __init__(
        self,
        registry: ToolRegistry,
        approval_policy: ApprovalPolicy,
        *,
        max_output_chars: int = 50_000,
    ) -> None:
        if type(max_output_chars) is not int or max_output_chars < 1:
            raise ValueError("max_output_chars must be a positive integer")
        self.registry = registry
        self.approval_policy = approval_policy
        self.max_output_chars = max_output_chars

    def _bound(self, content: str) -> str:
        """截断超限文本，截断标记计入字符上限。"""
        if len(content) <= self.max_output_chars:
            return content
        marker = "\n[truncated]"
        if self.max_output_chars < len(marker):
            return marker[: self.max_output_chars]
        return content[: self.max_output_chars - len(marker)] + marker

    def _failure(self, name: str, code: str, message: str) -> ToolResult:
        """生成长度受限的工具错误结果。"""
        safe_message = self._bound(message)
        return ToolResult(
            tool_name=name,
            content=self._bound("Tool error: " + message),
            success=False,
            error=safe_message,
            error_code=code,
        )

    def execute(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        """执行工具并返回结果；权限不足时停止并要求审批。"""
        try:
            tool = self.registry.get(name)
            if not isinstance(arguments, dict) or any(
                not isinstance(key, str) for key in arguments
            ):
                raise ToolError("arguments must be a JSON object", code="invalid_arguments")
            if isinstance(tool, ValidatedTool):
                arguments = tool.validate_arguments(arguments)
            if not self.approval_policy.allows(name, tool.permission, arguments):
                raise PermissionRequired(f"approval required for tool: {name}")
            content = tool.invoke(arguments)
            if not isinstance(content, str):
                return self._failure(name, "invalid_output", "tool must return text")
        except PermissionRequired:
            raise
        except ToolError as exc:
            return self._failure(name, exc.code, str(exc))
        except Exception:
            # 异常消息可能包含凭据、本机路径和第三方输出，不得直接返回。
            return self._failure(name, "execution_failed", "tool execution failed")
        return ToolResult(tool_name=name, content=self._bound(content))
