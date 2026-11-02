"""按预算选择发送给模型的任务状态，完整状态仍保存在检查点中。"""

import json
from collections.abc import Callable
from copy import deepcopy

from pydantic import JsonValue, TypeAdapter

from ..domain.task_status import TaskBoard


def project_board(
    board: TaskBoard, *, max_tokens: int, count: Callable[[str], int]
) -> dict[str, JsonValue]:
    """优先保留目标、限制和验收条件，再按整项加入未完成工作及近期事实。"""
    view: dict[str, JsonValue] = {
        "goal": board["goal"],
        "constraints": list(board["constraints"]),
        "criteria": list(board["criteria"]),
    }
    # 当前用户原文已由消息保护机制保留，不重复注入 current_request、ID 和时间。
    groups: list[tuple[str, list[JsonValue]]] = [
        ("blockers", list(board["blockers"])),
        ("next_action", [board["next_action"]] if board["next_action"] else []),
        (
            "items",
            [
                TypeAdapter(JsonValue).validate_python(item)
                for item in board["items"]
                if item["status"] != "completed"
            ],
        ),
        (
            "facts",
            [TypeAdapter(JsonValue).validate_python(fact) for fact in reversed(board["facts"])],
        ),
        (
            "items",
            [
                TypeAdapter(JsonValue).validate_python(item)
                for item in board["items"]
                if item["status"] == "completed"
            ],
        ),
    ]
    for key, values in groups:
        for value in values:
            candidate = deepcopy(view)
            existing = candidate.get(key)
            candidate[key] = [*(existing if isinstance(existing, list) else []), value]
            if count(json.dumps(candidate, ensure_ascii=False)) <= max_tokens:
                view = candidate
    return view
