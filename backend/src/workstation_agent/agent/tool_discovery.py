"""在模型请求前缩小工具定义集合，并阻止跳过发现的调用。"""

from collections.abc import Awaitable, Callable

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ModelRequest, ModelResponse
from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command
from pydantic import TypeAdapter, ValidationError

from ..context.state import ContextAgentState, ContextRuntime, DiscoveredTools

_SELECTION = TypeAdapter(DiscoveredTools)


class ToolDiscoveryMiddleware(AgentMiddleware[ContextAgentState, ContextRuntime]):
    """根据当前 Run 的检查点选择工具，过滤后的定义参与上下文预算。"""

    def __init__(self, deferred: frozenset[str]) -> None:
        self.deferred: frozenset[str] = deferred

    def _visible(self, value: object, context: object) -> frozenset[str]:
        if not isinstance(context, ContextRuntime) or not context.run_id or value is None:
            return frozenset()
        try:
            selection = _SELECTION.validate_python(value)
        except ValidationError:
            return frozenset()
        return frozenset(selection["names"][:8]) if selection["run_id"] == context.run_id else frozenset()

    def _request(self, request: ModelRequest[ContextRuntime]) -> ModelRequest[ContextRuntime]:
        visible = self._visible(request.state.get("discovered_tools"), request.runtime.context)
        return request.override(tools=[
            tool for tool in request.tools
            if isinstance(tool, dict) or tool.name not in self.deferred or tool.name in visible
        ])

    def available(self, names: list[str], selection: object, context: ContextRuntime) -> frozenset[str]:
        """返回摘要预算和模型请求可以看到的工具名。"""
        visible = self._visible(selection, context)
        return frozenset(name for name in names if name not in self.deferred or name in visible)

    def wrap_model_call(
        self, request: ModelRequest[ContextRuntime], handler: Callable[[ModelRequest[ContextRuntime]], ModelResponse],
    ) -> ModelResponse:
        """先筛选，再将请求交给上下文预算中间件。"""
        return handler(self._request(request))

    async def awrap_model_call(
        self, request: ModelRequest[ContextRuntime],
        handler: Callable[[ModelRequest[ContextRuntime]], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        """在异步图中按相同规则筛选工具。"""
        return await handler(self._request(request))

    def _denial(self, request: ToolCallRequest) -> ToolMessage | None:
        name = request.tool_call["name"]
        if name not in self.deferred:
            return None
        visible = self._visible(request.state.get("discovered_tools"), request.runtime.context)
        if name in visible:
            return None
        return ToolMessage("请先用 find_tools 发现该工具，再在下一轮调用。",
                           tool_call_id=request.tool_call["id"] or "", name=name, status="error")

    def wrap_tool_call(
        self, request: ToolCallRequest, handler: Callable[[ToolCallRequest], ToolMessage | Command],
    ) -> ToolMessage | Command:
        """拒绝猜测名称或与发现并行发起的未发现调用。"""
        denial = self._denial(request)
        return denial if denial is not None else handler(request)

    async def awrap_tool_call(
        self, request: ToolCallRequest, handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
    ) -> ToolMessage | Command:
        """已发现的调用继续经过原工具和审批执行器。"""
        denial = self._denial(request)
        return denial if denial is not None else await handler(request)
