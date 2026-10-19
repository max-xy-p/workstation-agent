"""通过官方 ToolRuntime 更新任务状态，写入前仍由 ToolExecutor 校验与授权。"""

from __future__ import annotations

from copy import deepcopy
from typing import Literal

from langchain.tools import ToolRuntime, tool
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, ToolMessage
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
    """约束任务项大小；模型报告完成后仍须由用户验收。"""

    id: str = Field(min_length=1, max_length=64)
    text: str = Field(min_length=1, max_length=200)
    status: Literal["pending", "in_progress", "pending_confirmation", "completed", "blocked"]
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
    """校验引用后更新进度；成功结果不等于任务已通过验收。"""

    name = "update_task_status"
    description = (
        "更新进度、事实、阻塞和下一步。报告完成时引用结果 ID；"
        "系统标为待用户确认，不能自行验收。省略的字段保持不变。"
    )
    permission = PermissionLevel.CONTROLLED_WRITE
    arguments_model = UpdateStatusArguments

    def __init__(self, board: TaskBoard, sources: dict[str, str]) -> None:
        self.board = deepcopy(board)
        self.sources = sources

    def validate_arguments(self, arguments: dict[str, JsonValue]) -> dict[str, JsonValue]:
        """保留实际提交的字段，避免参数默认值清空原有状态。"""
        return self._parse(arguments).model_dump(exclude_unset=True)

    def run(self, arguments: UpdateStatusArguments) -> str:
        """拒绝无凭据的完成项和无原文支持的已确认事实，不信任模型自述。"""
        items = self._validated_items(arguments.items)
        facts = self._validated_facts(arguments.facts)
        if any(len(text) > 300 for text in arguments.blockers):
            raise ToolError("阻塞说明过长", code="invalid_status")
        supplied = arguments.model_fields_set
        if "items" in supplied:
            self.board["items"] = items
        if "facts" in supplied:
            self.board["facts"] = facts
        if "blockers" in supplied:
            self.board["blockers"] = arguments.blockers
        if "next_action" in supplied:
            self.board["next_action"] = arguments.next_action
        self.board["revision"] = self.board.get("revision", 0) + 1
        self.board["updated_at"] = utc_now()
        return "任务状态已更新"

    def _validated_items(self, proposed: list[StatusItem]) -> list[TaskItem]:
        """验证唯一 ID 和来源，每个任务项单独判断是否保留用户验收。"""
        if len({item.id for item in proposed}) != len(proposed):
            raise ToolError("任务项 ID 重复", code="invalid_status")
        valid = {ref["id"] for ref in self.board["results"]}
        return [self._validated_item(item, valid) for item in proposed]

    def _validated_item(self, item: StatusItem, valid: set[str]) -> TaskItem:
        """模型提交 completed 时仅保留已经由用户确认且内容未变化的项目。"""
        if any(source not in valid for source in item.evidence):
            raise ToolError("任务项引用不存在或失败的结果", code="invalid_status")
        if item.status == "completed" and not item.evidence:
            raise ToolError("标记完成需要结果凭据", code="invalid_status")
        status = item.status
        if status == "completed":
            previous = next((old for old in self.board["items"] if old["id"] == item.id), None)
            unchanged = previous == {
                "id": item.id,
                "text": item.text,
                "status": "completed",
                "evidence": item.evidence,
            }
            status = "completed" if unchanged else "pending_confirmation"
        return {"id": item.id, "text": item.text, "status": status, "evidence": item.evidence}

    def _validated_facts(self, proposed: list[StatusFact]) -> list[TaskFact]:
        """确认事实必须逐字出现在当前来源中，验证失败时整个更新不写入。"""
        facts: list[TaskFact] = []
        for fact in proposed:
            if fact.confirmed and (
                fact.source not in self.sources or fact.text not in self.sources[fact.source]
            ):
                raise ToolError("已确认事实必须有当前可读原文支持", code="invalid_status")
            facts.append({"text": fact.text, "source": fact.source, "confirmed": fact.confirmed})
        return facts


def _parallel_status_update(messages: list[AnyMessage]) -> bool:
    """同一模型消息不能并行覆盖同一份任务状态。"""
    latest = next(
        (message for message in reversed(messages) if isinstance(message, AIMessage)), None
    )
    return (
        latest is not None
        and sum(call["name"] == "update_task_status" for call in latest.tool_calls) > 1
    )


def _status_sources(messages: list[AnyMessage]) -> dict[str, str]:
    """收集有身份的用户与工具原文，仅用于核对事实引用。"""
    return {
        (message.tool_call_id if isinstance(message, ToolMessage) else message.id): message.text
        for message in messages
        if message.id and isinstance(message, HumanMessage | ToolMessage)
    }


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
        parallel = _parallel_status_update(runtime.state["messages"])
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
        sources = _status_sources(runtime.state["messages"])
        operation = UpdateTaskStatusTool(board, sources)
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
