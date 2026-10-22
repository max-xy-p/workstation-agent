"""更新状态栏的可信字段和安全引用；工具失败不会成为完成凭据。"""

from __future__ import annotations

from collections.abc import Iterator
from copy import deepcopy
from hashlib import sha256

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, ToolCall, ToolMessage

from .selection import rag_evidence
from .state import ContextRuntime, ResultReference, TaskBoard, restore_board, utc_now


def initialize(
    messages: list[AnyMessage], saved: TaskBoard | None, runtime: ContextRuntime
) -> TaskBoard:
    """用户入口决定任务目标、验收条件和约束；模型更新接口不能修改它们。"""
    board: TaskBoard | None = None if runtime.new_task else restore_board(saved)
    user = next((m for m in reversed(messages) if isinstance(m, HumanMessage)), None)
    task = runtime.task or (user.text if user else "")
    if board is None:
        board = {
            "schema_version": 2,
            "revision": 0,
            "task_id": runtime.run_id or (user.id if user else "") or "task",
            "session_id": runtime.session_id,
            "updated_at": utc_now(),
            "goal": task,
            "current_request": task,
            "criteria": [],
            "constraints": [],
            "items": [],
            "facts": [],
            "results": [],
            "blockers": [],
            "next_action": "分析当前要求",
        }
    board = deepcopy(board)
    board["revision"] = board.get("revision", 0) + 1
    board["current_request"] = task
    board["criteria"] = list(dict.fromkeys([*board["criteria"], *runtime.criteria]))
    board["constraints"] = list(dict.fromkeys([*board["constraints"], *runtime.constraints]))
    board["updated_at"] = utc_now()
    runtime.board = board
    return board


def collect_results(
    board: TaskBoard,
    messages: list[AnyMessage],
    *,
    seen: set[str] | None = None,
) -> TaskBoard:
    """仅保存成功工具的调用 ID 和白名单来源字段，不复制工具正文或完整参数。"""
    board = deepcopy(board)
    refs: dict[str, ResultReference] = {item["id"]: item for item in board["results"]}
    calls = {
        call["id"]: call for m in messages if isinstance(m, AIMessage) for call in m.tool_calls
    }
    failures: list[str] = []
    for message in _unseen_tool_results(messages, seen):
        if message.status == "error":
            failures.append(f"工具 {message.name or 'tool'} 执行失败，调用 {message.tool_call_id}")
            continue
        reference = _result_reference(message, calls.get(message.tool_call_id))
        if reference is not None:
            _replace_reference(board, refs, reference)
    # 已有任务项引用的凭据不会因新增结果被删除，其余保留最近 32 项。
    used = {source for item in board["items"] for source in item["evidence"]}
    values = list(refs.values())
    recent = {item["id"] for item in values[-32:]}
    board["results"] = [item for item in values if item["id"] in used | recent]
    board["blockers"] = list(dict.fromkeys([*board["blockers"], *failures]))[-8:]
    board["updated_at"] = utc_now()
    return board


def _unseen_tool_results(
    messages: list[AnyMessage], seen: set[str] | None
) -> Iterator[ToolMessage]:
    """忽略内部状态工具和已处理消息，避免重复记录错误或引用。"""
    for message in messages:
        if not isinstance(message, ToolMessage) or message.name == "update_task_status":
            continue
        identity = message.id or message.tool_call_id
        if seen is not None:
            if identity in seen:
                continue
            seen.add(identity)
        yield message


def _result_reference(message: ToolMessage, call: ToolCall | None) -> ResultReference | None:
    """仅从实际成功结果提取安全位置和版本；恢复占位消息不能成为新凭据。"""
    if call is None or "[工具结果未持久化" in message.text:
        return None
    location = ""
    version = sha256(message.text.encode()).hexdigest()
    evidence = rag_evidence(message)
    if evidence:
        location = "; ".join(item["citation"] for item in evidence)[:1000]
        version = evidence[0]["checksum"]
    elif message.name == "read_text_file":
        path = call["args"].get("path")
        if isinstance(path, str):
            location = path[:500] + f"@offset={call['args'].get('offset', 0)}"
    return {
        "id": message.tool_call_id,
        "tool": message.name or "tool",
        "location": location,
        "version": version,
    }


def _replace_reference(
    board: TaskBoard, refs: dict[str, ResultReference], reference: ResultReference
) -> None:
    """引用来源改变时撤销旧事实和验收，再保存新引用。"""
    identifier = reference["id"]
    previous = refs.get(identifier)
    if previous is not None and previous != reference:
        board["facts"] = [fact for fact in board["facts"] if fact["source"] != identifier]
        for item in board["items"]:
            if identifier in item["evidence"] and item["status"] == "completed":
                item["status"] = "pending_confirmation"
        board["revision"] += 1
    refs[identifier] = reference


def invalidate_memory_sources(board: TaskBoard, invalid: set[str]) -> TaskBoard:
    """记忆变更后移除状态栏中的旧引用和依赖事实，防止缓存把旧值重新带回模型。"""
    board = deepcopy(board)
    board["facts"] = [fact for fact in board["facts"] if fact["source"] not in invalid]
    board["results"] = [ref for ref in board["results"] if ref["id"] not in invalid]
    for item in board["items"]:
        previous = item["evidence"]
        item["evidence"] = [source for source in previous if source not in invalid]
        if previous and not item["evidence"] and item["status"] == "completed":
            item["status"] = "blocked"
            board["blockers"] = [*board["blockers"], "任务项的记忆来源已变化，需要重新核对"][-8:]
    return board


def confirm_items(
    board: TaskBoard, *, task_id: str, revision: int, item_ids: tuple[str, ...]
) -> TaskBoard:
    """由用户入口验收指定版本的任务项；模型工具不能调用此函数。"""
    if board["task_id"] != task_id or board.get("revision", 0) != revision:
        raise ValueError("任务状态已变化，请重新查看后确认")
    if not item_ids or len(set(item_ids)) != len(item_ids):
        raise ValueError("请选择不重复的待确认任务项")
    selected = [item for item in board["items"] if item["id"] in item_ids]
    if len(selected) != len(item_ids) or any(
        item["status"] != "pending_confirmation" for item in selected
    ):
        raise ValueError("只能确认当前任务中等待验收的项目")
    result = deepcopy(board)
    for item in result["items"]:
        if item["id"] in item_ids:
            item["status"] = "completed"
    result["revision"] = revision + 1
    result["updated_at"] = utc_now()
    return result
