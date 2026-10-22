"""向 CLI、MCP 服务和同进程 Agent 提供统一的工具调用入口。"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from ..tools.contracts import ToolDefinition
from ..tools import ToolExecutor, ToolResult
from ..tools.workflow import ToolExecutionUpdate, ToolWorkflow


class ToolApplication:
    """提供统一的工具发现、执行和进度入口。"""

    def __init__(self, executor: ToolExecutor) -> None:
        self.executor = executor
        self.workflow = ToolWorkflow(executor)

    def list_tools(self) -> tuple[ToolDefinition, ...]:
        """返回工具定义列表。"""
        return self.executor.registry.definitions

    def tool_catalog(self) -> list[dict[str, Any]]:
        """返回包含权限说明的工具目录。"""
        return [
            {
                "name": definition.name,
                "description": definition.description,
                "permission": self.executor.registry.get(definition.name).permission.name,
                "parameters": definition.parameters,
            }
            for definition in self.list_tools()
        ]

    def execute_tool(self, name: str, arguments: object) -> ToolResult:
        """执行一次工具请求并返回结果。"""
        return self.workflow.run(name, arguments)

    def stream_tool(self, name: str, arguments: object) -> Iterator[ToolExecutionUpdate]:
        """执行工具并返回节点进度。"""
        return self.workflow.stream(name, arguments)
