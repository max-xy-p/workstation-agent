"""使用官方工厂创建 Agent 执行图。"""

from collections.abc import Sequence
from typing import Any

from langchain.agents import AgentState, create_agent
from langchain.agents.middleware import (
    AgentMiddleware,
    ModelCallLimitMiddleware,
    ToolCallLimitMiddleware,
)
from langchain.agents.middleware.types import InputAgentState, OutputAgentState
from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph.state import CompiledStateGraph

from ..context.budget import resolve_settings
from ..context.config import ContextSettings
from ..context.state import ContextAgentState, ContextRuntime
from ..tools.task_status import task_status_tool
from .context import ContextMiddleware
from .prompts import SYSTEM_PROMPT
from .rag_guard import RagSearchGuard
from .run_boundary import RunBoundary


def build_agent(
    model: BaseChatModel,
    tools: Sequence[BaseTool],
    *,
    max_model_calls: int = 8,
    max_tool_calls: int = 32,
    max_search_calls: int = 6,
    checkpointer: BaseCheckpointSaver | None = None,
    summary_model: BaseChatModel | None = None,
    context_settings: ContextSettings | None = None,
) -> CompiledStateGraph[AgentState, ContextRuntime, InputAgentState, OutputAgentState]:
    """创建官方循环；所有运行都经过完整上下文预算，未知模型必须配置容量。"""
    settings = resolve_settings(context_settings or ContextSettings(), model)
    settings.input_budget()
    if any(tool.name == "update_task_status" for tool in tools):
        raise ValueError("update_task_status 是 Agent 内部保留的工具名称")
    tools = [*tools, task_status_tool()]
    context_middleware = ContextMiddleware(model, tools, settings, summary_model)
    # 官方工厂把多种中间件状态合并为运行时 schema；只有此组装边界使用 Any 描述合并状态。
    middleware: list[AgentMiddleware[Any, ContextRuntime]] = [
        RunBoundary(),
        ModelCallLimitMiddleware[Any, ContextRuntime](
            run_limit=max_model_calls, thread_limit=max_model_calls, exit_behavior="error"
        ),
        ToolCallLimitMiddleware[Any, ContextRuntime](
            run_limit=max_tool_calls, thread_limit=max_tool_calls, exit_behavior="error"
        ),
        ToolCallLimitMiddleware[Any, ContextRuntime](
            tool_name="search_documents",
            run_limit=max_search_calls,
            thread_limit=max_search_calls,
            exit_behavior="error",
        ),
        RagSearchGuard(),
        context_middleware,
    ]

    return create_agent(
        model=model,
        tools=tools,
        system_prompt=SYSTEM_PROMPT,
        checkpointer=checkpointer if checkpointer is not None else InMemorySaver(),
        state_schema=ContextAgentState,
        context_schema=ContextRuntime,
        middleware=middleware,
        name="workstation_agent",
    ).with_config(recursion_limit=max_model_calls * 10 + 10)
