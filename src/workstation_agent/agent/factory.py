"""使用官方工厂创建 Agent 执行图。"""

from langchain.agents import create_agent
from langchain.agents.middleware import ModelCallLimitMiddleware, ToolCallLimitMiddleware
from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool
from langgraph.graph.state import CompiledStateGraph

from .context import ContextMiddleware
from .prompts import SYSTEM_PROMPT
from .rag_guard import RagSearchGuard


def build_agent(
    model: BaseChatModel,
    tools: list[BaseTool],
    *,
    max_model_calls: int = 8,
    max_tool_calls: int = 32,
    max_search_calls: int = 6,
    checkpointer=None,
    summary_model=None,
    summary_tokens=12000,
) -> CompiledStateGraph:
    """创建模型与工具调用流程，并限制调用次数。"""
    extra = []
    if summary_model is not None:
        from langchain.agents.middleware import SummarizationMiddleware

        extra.append(
            SummarizationMiddleware(
                summary_model, trigger=("tokens", summary_tokens), keep=("messages", 8)
            )
        )
    elif checkpointer is not None:
        from .history import HistoryTrimMiddleware

        extra.append(HistoryTrimMiddleware(summary_tokens))
    return create_agent(
        model=model,
        tools=tools,
        system_prompt=SYSTEM_PROMPT,
        checkpointer=checkpointer,
        middleware=[
            *extra,
            ContextMiddleware(),
            ModelCallLimitMiddleware(run_limit=max_model_calls, exit_behavior="error"),
            ToolCallLimitMiddleware(run_limit=max_tool_calls, exit_behavior="error"),
            ToolCallLimitMiddleware(
                tool_name="search_documents", run_limit=max_search_calls, exit_behavior="error"
            ),
            RagSearchGuard(),
        ],
        name="workstation_agent",
    ).with_config(recursion_limit=max_model_calls * 10 + 10)
