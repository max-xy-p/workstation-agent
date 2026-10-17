"""通过官方 ToolRuntime 更新任务状态，写入前仍由 ToolExecutor 校验与授权。"""

from __future__ import annotations

from copy import deepcopy
from typing import Literal

from langchain.tools import ToolRuntime, tool
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import BaseTool
from langgraph.types import Command
from pydantic import Field, JsonValue

from ..context.state import (
    ContextAgentState,
    ContextRuntime,
    TaskBoard,
    TaskFact,
    TaskItem,
    utc_now,
)
from ..safety import AllowSafeReadsPolicy, PermissionLevel
from .contracts import PydanticTool, ToolArguments, ToolError
from .executor import ToolExecutor
from .registry import ToolRegistry


class StatusItem(ToolArguments):
    """约束任务项大小；完成项需要成功结果的调用 ID 或用户消息 ID。"""

    id: str = Field(min_length=1, max_length=64)
    text: str = Field(min_length=1, max_length=200)
    status: Literal["pending", "in_progress", "completed", "blocked"]
    evidence: list[str] = Field(default_factory=list, max_length=8)


class StatusFact(ToolArguments):
    """确认事实必须逐字出现在引用的当前可用来源中，否则只能标为待验证。"""

    text: str = Field(min_length=1, max_length=300)
    source: str = Field(default="", max_length=128)
    confirmed: bool = False


class UpdateStatusArguments(ToolArguments):
    """只允许模型更新进度字段，不提供目标、约束和用户验收条件的修改入口。"""

    items: list[StatusItem] = Field(default_factory=list, max_length=16)
    facts: list[StatusFact] = Field(default_factory=list, max_length=12)
    blockers: list[str] = Field(default_factory=list, max_length=8)
    next_action: str = Field(default="", max_length=300)


class RunStatePolicy(AllowSafeReadsPolicy):
    """用户授权运行任务时允许更新该任务的内存状态，不扩大外部工具权限。"""

    def allows(
        self, tool_name: str, permission: PermissionLevel, arguments: dict[str, JsonValue]
    ) -> bool:
        return tool_name == "update_task_status" and permission == PermissionLevel.CONTROLLED_WRITE


class UpdateTaskStatusTool(PydanticTool[UpdateStatusArguments]):
    """校验结果凭据后更新运行内状态；是否写检查点由已有会话授权决定。"""

    name = "update_task_status"
    description = (
        "更新进度、事实、阻塞和下一步。完成项必须引用成功工具调用 ID；目标和约束不可修改。"
    )
    permission = PermissionLevel.CONTROLLED_WRITE
    arguments_model = UpdateStatusArguments

    def __init__(self, board: TaskBoard, sources: dict[str, str], confirmations: set[str]) -> None:
        self.board = deepcopy(board)
        self.sources = sources
        self.confirmations = confirmations

    def run(self, arguments: UpdateStatusArguments) -> str:
        """拒绝无凭据的完成项和无原文支持的已确认事实，不信任模型自述。"""
        valid = {ref["id"] for ref in self.board["results"]} | self.confirmations
        if len({item.id for item in arguments.items}) != len(arguments.items):
            raise ToolError("任务项 ID 重复", code="invalid_status")
        items: list[TaskItem] = []
        for item in arguments.items:
            if any(source not in valid for source in item.evidence):
                raise ToolError("任务项引用不存在或失败的结果", code="invalid_status")
            if item.status == "completed" and not item.evidence:
                raise ToolError("标记完成需要结果凭据", code="invalid_status")
            items.append(
                {"id": item.id, "text": item.text, "status": item.status, "evidence": item.evidence}
            )
        facts: list[TaskFact] = []
        for fact in arguments.facts:
            if fact.confirmed and (
                fact.source not in self.sources or fact.text not in self.sources[fact.source]
            ):
                raise ToolError("已确认事实必须有当前可读原文支持", code="invalid_status")
            facts.append({"text": fact.text, "source": fact.source, "confirmed": fact.confirmed})
        if any(len(text) > 300 for text in arguments.blockers):
            raise ToolError("阻塞说明过长", code="invalid_status")
        self.board.update(
            items=items,
            facts=facts,
            blockers=arguments.blockers,
            next_action=arguments.next_action,
            updated_at=utc_now(),
        )
        return "任务状态已更新"


def task_status_tool() -> BaseTool:
    """使用框架注入状态和调用 ID，模型无法指定另一任务或会话。"""

    # 外层使用 JSON Schema，避免把框架注入的 runtime 当作业务参数校验。
    # 此处仅路由工具 JSON；ToolExecutor 随即将参数严格转换为 UpdateStatusArguments。
    @tool("update_task_status", args_schema=UpdateStatusArguments.model_json_schema())
    def update_task_status(
        runtime: ToolRuntime[ContextRuntime, ContextAgentState],
        **arguments: JsonValue,
    ) -> Command:
        """更新进度与来源。仅在实际结果支持时标记完成，简单问答无需使用。"""
        board = runtime.state.get("task_board")
        latest = next(
            (m for m in reversed(runtime.state["messages"]) if isinstance(m, AIMessage)), None
        )
        parallel = (
            latest is not None
            and sum(call["name"] == "update_task_status" for call in latest.tool_calls) > 1
        )
        if board is None or parallel:
            return Command(
                update={
                    "messages": [
                        ToolMessage(
                            "同一轮只能更新一次任务状态" if parallel else "任务状态尚未初始化",
                            name="update_task_status",
                            status="error",
                            tool_call_id=runtime.tool_call_id or "",
                        )
                    ]
                }
            )
        sources: dict[str, str] = {
            (m.tool_call_id if isinstance(m, ToolMessage) else m.id): m.text
            for m in runtime.state["messages"]
            if m.id and isinstance(m, HumanMessage | ToolMessage)
        }
        confirmations = {
            m.id
            for m in runtime.state["messages"]
            if isinstance(m, HumanMessage)
            and m.id
            and m.text.strip() in {"确认完成", "已完成", "确认已完成"}
        }
        operation = UpdateTaskStatusTool(board, sources, confirmations)
        result = ToolExecutor(ToolRegistry((operation,)), RunStatePolicy()).execute(
            "update_task_status",
            dict(arguments),
        )
        message = ToolMessage(
            result.content,
            name="update_task_status",
            status="success" if result.success else "error",
            tool_call_id=runtime.tool_call_id or "",
        )
        if not result.success:
            return Command(update={"messages": [message]})
        if runtime.context:
            runtime.context.board = operation.board
        return Command(update={"task_board": operation.board, "messages": [message]})

    return update_task_status
