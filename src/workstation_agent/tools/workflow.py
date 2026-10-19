"""用 LangGraph 安排一次工具调用的顺序：先检查请求，再执行工具。"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from pydantic import ValidationError

from ..safety.permissions import PermissionRequired
from .contracts import ToolRequest
from .executor import ToolExecutor, ToolResult


class ToolWorkflowState(TypedDict, total=False):
    """保存单次工具调用在图中传递的请求和结果。"""

    # 要调用的工具名称。
    name: str
    # 未经请求校验的原始参数。
    raw_arguments: object
    # 通过格式校验的调用请求。
    request: ToolRequest
    # 最终工具结果，也用于结束失败请求。
    result: ToolResult


@dataclass(frozen=True, slots=True)
class ToolExecutionUpdate:
    """通知已完成的节点及其产生的结果。"""

    # 刚完成的图节点名称。
    node: str
    # 该节点产生的结果，没有时为 None。
    result: ToolResult | None = None


class ToolWorkflow:
    """用 LangGraph 编排请求校验和工具执行。"""

    def __init__(self, executor: ToolExecutor) -> None:
        self.executor = executor
        builder = StateGraph(ToolWorkflowState)
        builder.add_node("validate_request", self._validate_request)
        builder.add_node("execute_tool", self._execute_tool)
        builder.add_edge(START, "validate_request")
        builder.add_conditional_edges(
            "validate_request", self._route, {"execute_tool": "execute_tool", "finish": END}
        )
        builder.add_edge("execute_tool", END)
        # 单次工具校验流不保存检查点，避免继承外层会话并持久化原始工具结果。
        self.graph = builder.compile(checkpointer=False)

    def _validate_request(self, state: ToolWorkflowState) -> dict[str, Any]:
        """检查请求格式，错误时生成失败结果。"""
        try:
            request = ToolRequest.model_validate(
                {
                    "name": state["name"],
                    "arguments": state["raw_arguments"],
                }
            )
        except ValidationError:
            return {
                "result": ToolResult(
                    tool_name=state["name"],
                    content="Tool error: arguments must be a JSON object",
                    success=False,
                    error="arguments must be a JSON object",
                    error_code="invalid_arguments",
                )
            }
        return {"request": request}

    def _route(self, state: ToolWorkflowState) -> str:
        """决定结束流程还是执行工具。"""
        return "finish" if "result" in state else "execute_tool"

    def _execute_tool(self, state: ToolWorkflowState) -> dict[str, ToolResult]:
        """调用执行器，并将审批要求转换成工具结果。"""
        request = state["request"]
        try:
            result = self.executor.execute(request.name, request.arguments)
        except PermissionRequired:
            result = ToolResult(
                tool_name=request.name,
                content="Tool error: approval required",
                success=False,
                error="approval required",
                error_code="permission_required",
            )
        return {"result": result}

    def run(self, name: str, arguments: object) -> ToolResult:
        """执行单次工具请求并返回结果。"""
        return self.graph.invoke({"name": name, "raw_arguments": arguments})["result"]

    def stream(self, name: str, arguments: object) -> Iterator[ToolExecutionUpdate]:
        """逐步返回节点完成通知和工具结果。"""
        for update in self.graph.stream(
            {"name": name, "raw_arguments": arguments}, stream_mode="updates"
        ):
            for node, values in update.items():
                yield ToolExecutionUpdate(node=node, result=values.get("result"))
