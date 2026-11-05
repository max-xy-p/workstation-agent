"""把工作站工具包装成 LangChain 的 StructuredTool，让官方 Agent 可以调用它们。"""

from collections.abc import Awaitable, Callable
from typing import Protocol

from langchain_core.tools import StructuredTool, ToolException
from langgraph.types import interrupt
from pydantic import JsonValue, TypeAdapter

from ..safety import PermissionRequired
from .approval import ApprovalDecision, ApprovalRequest
from .contracts import ToolDefinition
from .execution import ToolGrant, approved_call, check_execution, execute_blocking
from .executor import ToolResult


class ToolService(Protocol):
    """约定工具发现和执行的应用接口。"""

    def list_tools(self) -> tuple[ToolDefinition, ...]: ...
    def execute_tool(self, name: str, arguments: object) -> ToolResult: ...


_RESULT = TypeAdapter(ToolResult)


def result_payload(result: ToolResult) -> tuple[str, dict[str, JsonValue]]:
    """转换工具结果供框架使用；普通错误反馈模型，审批要求停止任务。"""
    text = _RESULT.dump_json(result).decode("utf-8")
    if result.error_code == "permission_required":
        raise PermissionRequired("工具需要审批")
    if not result.success:
        raise ToolException(text)
    return text, _RESULT.dump_python(result, mode="json")


def make_langchain_tool(
    definition: ToolDefinition,
    invoke: Callable[[dict[str, JsonValue]], Awaitable[ToolResult]],
) -> StructuredTool:
    """将工具定义和调用函数包装成官方 StructuredTool。"""

    async def call(**arguments: JsonValue) -> tuple[str, dict[str, JsonValue]]:
        check_execution()
        result = await invoke(arguments)
        if result.error_code == "permission_required":
            grant = ToolGrant.for_call(definition.name, arguments, context=definition.approval_context)
            request = result.approval or ApprovalRequest(
                name=definition.name, arguments=arguments, fingerprint=grant.fingerprint
            )
            if request.name != definition.name:
                raise ValueError("审批工具与当前调用不匹配")
            grant = ToolGrant(name=request.name, fingerprint=request.fingerprint)
            # 官方中断从工具节点恢复；只在用户批准该指纹后临时授予本次调用。
            decision = ApprovalDecision.model_validate(interrupt(request.model_dump()))
            if decision.fingerprint != grant.fingerprint:
                raise ValueError("审批参数已变化")
            if decision.action == "reject":
                result = ToolResult(
                    definition.name,
                    "用户拒绝执行该操作，请停止重试",
                    False,
                    "用户拒绝审批",
                    "approval_rejected",
                )
            else:
                check_execution()
                with approved_call(grant):
                    result = await invoke(arguments)
        return result_payload(result)

    return StructuredTool.from_function(
        coroutine=call,
        name=definition.name,
        description=definition.description,
        args_schema=definition.parameters,
        response_format="content_and_artifact",
        handle_tool_error=True,
    )


def local_tools(application: ToolService) -> list[StructuredTool]:
    """将同进程工具入口包装成 LangChain 工具。"""

    def adapt(definition: ToolDefinition) -> StructuredTool:
        async def invoke(arguments: dict[str, JsonValue]) -> ToolResult:
            return await execute_blocking(
                lambda: application.execute_tool(definition.name, arguments)
            )

        return make_langchain_tool(definition, invoke)

    return [adapt(definition) for definition in application.list_tools()]
