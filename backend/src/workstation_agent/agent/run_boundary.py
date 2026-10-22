"""让官方调用限制跨审批恢复生效，同时在新用户任务开始时重置计数。"""

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware
from langgraph.runtime import Runtime

from ..context.state import ContextRuntime


class RunBoundary(AgentMiddleware[AgentState, ContextRuntime]):
    """仅在新任务入口清空官方持久计数；Command 恢复不会经过 before_agent。"""

    def before_agent(
        self, state: AgentState, runtime: Runtime[ContextRuntime]
    ) -> dict[str, int | dict[str, int]]:
        """run_limit 按图调用计数；用官方 thread_limit 补齐一次任务多次恢复的预算。"""
        return {"thread_model_call_count": 0, "thread_tool_call_count": {}}

    async def abefore_agent(
        self, state: AgentState, runtime: Runtime[ContextRuntime]
    ) -> dict[str, int | dict[str, int]]:
        """异步入口采用相同的新任务计数规则。"""
        return self.before_agent(state, runtime)
