"""保存可用工具，让调用方通过名称找到对应的工具对象。"""

from __future__ import annotations

from .contracts import Tool, ToolDefinition, ToolError


class ToolRegistry:
    """按名称保存和查找工具。"""

    def __init__(self, tools: tuple[Tool, ...] = ()) -> None:
        self._tools = {tool.definition.name: tool for tool in tools}
        if len(self._tools) != len(tools):
            raise ValueError("tool names must be unique")

    @property
    def definitions(self) -> tuple[ToolDefinition, ...]:
        """返回全部工具的说明。"""
        return tuple(tool.definition for tool in self._tools.values())

    def get(self, name: str) -> Tool:
        """按名称查找工具，不存在时抛出错误。"""
        tool = self._tools.get(name)
        if tool is None:
            raise ToolError("unknown tool; use tools to list available names", code="unknown_tool")
        return tool
