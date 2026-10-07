from pathlib import Path

from .builtin import ListFilesTool, ReadTextFileTool
from .contracts import PydanticTool, Tool, ToolArguments, ToolError
from .executor import ToolExecutor, ToolResult
from .registry import ToolRegistry


def default_tools(workspace: Path, *, max_output_chars: int = 50_000) -> ToolRegistry:
    """注册默认的目录列表和文本读取工具。"""
    return ToolRegistry((ListFilesTool(workspace), ReadTextFileTool(workspace, max_chars=max_output_chars)))


__all__ = [
    "ListFilesTool",
    "ReadTextFileTool",
    "PydanticTool",
    "ToolArguments",
    "Tool",
    "ToolError",
    "ToolExecutor",
    "ToolRegistry",
    "ToolResult",
    "default_tools",
]
