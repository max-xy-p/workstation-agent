"""使用官方工厂创建 Agent 执行图。"""

from langchain.agents import create_agent
from langchain.agents.middleware import ModelCallLimitMiddleware, ToolCallLimitMiddleware
from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool
from langgraph.graph.state import CompiledStateGraph


def build_agent(
    model: BaseChatModel, tools: list[BaseTool], *, max_model_calls: int = 8,
    max_tool_calls: int = 32,
) -> CompiledStateGraph:
    """创建模型与工具调用流程，并限制调用次数。"""
    return create_agent(
        model=model, tools=tools,
        middleware=[
            ModelCallLimitMiddleware(run_limit=max_model_calls, exit_behavior="error"),
            ToolCallLimitMiddleware(run_limit=max_tool_calls, exit_behavior="error"),
        ],
        name="workstation_agent",
    ).with_config(recursion_limit=max_model_calls * 10 + 10)
