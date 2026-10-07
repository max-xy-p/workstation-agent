from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from .application import AgentApplication, ToolApplication
from .config import Settings
from .safety import AllowSafeReadsPolicy
from .storage import SQLiteRunRepository
from .tools import ToolExecutor, ToolRegistry, default_tools


@dataclass(frozen=True, slots=True)
class Infrastructure:
    """保存工具目录和任务存储对象。"""
    # 可用工具目录。
    tools: ToolRegistry
    # SQLite 任务记录存储。
    runs: SQLiteRunRepository


def build_infrastructure(settings: Settings) -> Infrastructure:
    """创建工具目录和 SQLite 任务存储。"""
    return Infrastructure(
        tools=default_tools(settings.workspace, max_output_chars=settings.tool_output_chars),
        runs=SQLiteRunRepository(settings.database_path),
    )


def build_tool_application(settings: Settings) -> ToolApplication:
    """创建工具调用入口并配置默认只读权限。"""
    registry = default_tools(settings.workspace, max_output_chars=settings.tool_output_chars)
    executor = ToolExecutor(
        registry, AllowSafeReadsPolicy(), max_output_chars=settings.tool_output_chars
    )
    return ToolApplication(executor)


@asynccontextmanager
async def build_agent_application(
    settings: Settings, *, transport: str = "mcp",
) -> AsyncIterator["AgentApplication"]:
    """组装模型、工具和 Agent 图，并管理 MCP 连接。"""
    from .agent.factory import build_agent
    from .models.providers.openai import build_chat_model
    from .tools.langchain import local_tools
    from .tools.mcp_client import workstation_tools

    model = build_chat_model(settings)

    def application(tools) -> AgentApplication:
        graph = build_agent(
            model, tools, max_model_calls=settings.max_steps,
            max_tool_calls=settings.max_tool_calls,
        )
        return AgentApplication(graph)

    if transport == "mcp":
        async with workstation_tools(settings) as tools:
            yield application(tools)
    elif transport == "local":
        yield application(local_tools(build_tool_application(settings)))
    else:
        raise ValueError("未知工具传输方式")
