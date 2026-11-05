"""在工具真正操作文件等资源前检查参数和权限，并统一处理返回结果。"""

from __future__ import annotations

from typing import Any

from pydantic import ConfigDict, Field, JsonValue
from pydantic.dataclasses import dataclass

from ..safety.permissions import ApprovalPolicy, PermissionLevel, PermissionRequired
from .approval import ApprovalRequest, PlannedApprovalRequired
from .contracts import AsyncTool, AsyncValueTool, PreparingTool, ToolError, ValidatedTool
from .execution import GRANT, ToolGrant, check_execution
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
    approval: ApprovalRequest | None = Field(default=None, exclude_if=lambda value: value is None)


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
            check_execution()
            tool = self.registry.get(name)
            if not isinstance(arguments, dict) or any(
                not isinstance(key, str) for key in arguments
            ):
                raise ToolError("arguments must be a JSON object", code="invalid_arguments")
            requested = ToolGrant.for_call(name, arguments, context=tool.definition.approval_context)
            if isinstance(tool, ValidatedTool):
                arguments = tool.validate_arguments(arguments)
            prepared = None
            if isinstance(tool, PreparingTool):
                if not self.approval_policy.allows(name, PermissionLevel.SAFE_READ, arguments):
                    raise ToolError("未授权读取计划所需资料", code="permission_denied")
                prepared = tool.prepare_call(arguments)
                requested = ToolGrant.for_call(name, arguments, context=prepared.fingerprint)
                grant = GRANT.get()
                if grant is not None and grant.name == name and grant != requested:
                    raise ToolError("操作计划已变化，请重新准备", code="plan_changed")
                if prepared.unchanged:
                    check_execution()
                    return ToolResult(name, self._bound(prepared.commit()))
            if GRANT.get() != requested and not self.approval_policy.allows(
                name, tool.permission, arguments
            ):
                if prepared is not None:
                    raise PlannedApprovalRequired(ApprovalRequest(
                        name=name, arguments=prepared.summary, fingerprint=requested.fingerprint,
                    ))
                raise PermissionRequired(f"approval required for tool: {name}")
            check_execution()
            content = prepared.commit() if prepared is not None else tool.invoke(arguments)
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

    async def execute_async(self, tool: AsyncTool, arguments: dict[str, JsonValue]) -> ToolResult:
        """校验已发现的异步工具后授权，再发出远程请求；审批前不会试调用远端。"""
        name = tool.definition.name
        try:
            content = await self.execute_value(tool, arguments)
            return ToolResult(name, self._bound(content))
        except PlannedApprovalRequired as exc:
            return ToolResult(name, "操作计划需要审批", False, "操作计划需要审批",
                              "permission_required", exc.request)
        except PermissionRequired:
            raise
        except ToolError as exc:
            return self._failure(name, exc.code, str(exc))
        except Exception:
            return self._failure(name, "execution_failed", "远程工具执行失败")

    async def execute_value[ResultT](self, tool: AsyncValueTool[ResultT], arguments: dict[str, JsonValue]) -> ResultT:
        """授权后保留原始结果类型；应用处理不走模型输出截断，错误仍不包含底层异常。"""
        try:
            check_execution()
            requested = ToolGrant.for_call(tool.definition.name, arguments, context=tool.definition.approval_context)
            validated = tool.validate_arguments(arguments)
            if GRANT.get() != requested and not self.approval_policy.allows(tool.definition.name, tool.permission, validated):
                raise PermissionRequired("工具需要审批")
            check_execution()
            return await tool.ainvoke(validated)
        except (PermissionRequired, ToolError):
            raise
        except Exception:
            raise ToolError("异步工具执行失败", code="execution_failed") from None
