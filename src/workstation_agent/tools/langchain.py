"""把工作站工具包装成 LangChain 的 StructuredTool，让官方 Agent 可以调用它们。"""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Protocol

from langchain_core.tools import StructuredTool, ToolException
from pydantic import TypeAdapter

from ..safety import PermissionRequired
from .contracts import ToolDefinition
from .executor import ToolResult


class ToolService(Protocol):
    """约定工具发现和执行的应用接口。"""
    def list_tools(self) -> tuple[ToolDefinition, ...]: ...
    def execute_tool(self, name: str, arguments: object) -> ToolResult: ...


_RESULT = TypeAdapter(ToolResult)


def result_payload(result: ToolResult) -> tuple[str, dict]:
    """转换工具结果供框架使用；普通错误反馈模型，审批要求停止任务。"""
    text = _RESULT.dump_json(result).decode("utf-8")
    if result.error_code == "permission_required":
        raise PermissionRequired("工具需要审批")
    if not result.success:
        raise ToolException(text)
    return text, _RESULT.dump_python(result, mode="json")


def make_langchain_tool(
    definition: ToolDefinition, invoke: Callable[[dict], Awaitable[ToolResult]],
) -> StructuredTool:
    """将工具定义和调用函数包装成官方 StructuredTool。"""
    async def call(**arguments):
        return result_payload(await invoke(arguments))

    return StructuredTool.from_function(
        coroutine=call, name=definition.name, description=definition.description,
        args_schema=definition.parameters, response_format="content_and_artifact",
        handle_tool_error=True,
    )


def local_tools(application: ToolService) -> list[StructuredTool]:
    """将同进程工具入口包装成 LangChain 工具。"""
    def adapt(definition: ToolDefinition) -> StructuredTool:
        async def invoke(arguments: dict) -> ToolResult:
            return await asyncio.to_thread(application.execute_tool, definition.name, arguments)
        return make_langchain_tool(definition, invoke)

    return [adapt(definition) for definition in application.list_tools()]
