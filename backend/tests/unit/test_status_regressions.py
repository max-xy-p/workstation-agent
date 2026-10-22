"""复现任务状态误报完成和局部更新丢失数据的问题。"""

import json
import unittest
from copy import deepcopy

from workstation_agent.context.projection import project_board
from workstation_agent.context.state import ContextRuntime, restore_board
from workstation_agent.context.status import confirm_items, initialize
from workstation_agent.tools.executor import ToolExecutor
from workstation_agent.tools.registry import ToolRegistry
from workstation_agent.tools.task_status import RunStatePolicy, UpdateTaskStatusTool


class StatusRegressionTests(unittest.TestCase):
    def operation(self) -> UpdateTaskStatusTool:
        board = initialize([], None, ContextRuntime(run_id="task-1", task="修改代码并通过测试"))
        board["items"] = [
            {"id": "test", "text": "运行测试", "status": "in_progress", "evidence": []}
        ]
        board["facts"] = [{"text": "使用 Python", "source": "user", "confirmed": True}]
        board["blockers"] = ["尚未运行测试"]
        board["results"] = [{"id": "listing", "tool": "list_files", "location": "", "version": ""}]
        return UpdateTaskStatusTool(board, {})

    def execute(self, operation: UpdateTaskStatusTool, values: dict) -> None:
        result = ToolExecutor(ToolRegistry((operation,)), RunStatePolicy()).execute(
            "update_task_status", values
        )
        self.assertTrue(result.success, result.error)

    def test_partial_update_keeps_other_fields(self) -> None:
        operation = self.operation()
        self.execute(operation, {"next_action": "开始测试"})
        self.assertEqual(len(operation.board["items"]), 1)
        self.assertEqual(len(operation.board["facts"]), 1)
        self.assertEqual(operation.board["blockers"], ["尚未运行测试"])

    def test_unrelated_success_is_not_verified_completion(self) -> None:
        operation = self.operation()
        self.execute(
            operation,
            {
                "items": [
                    {
                        "id": "test",
                        "text": "测试通过",
                        "status": "completed",
                        "evidence": ["listing"],
                    }
                ]
            },
        )
        self.assertEqual(operation.board["items"][0]["status"], "pending_confirmation")

    def test_old_confirmation_does_not_authorize_new_completion(self) -> None:
        operation = self.operation()
        operation.sources["old-user-message"] = "确认完成"
        result = ToolExecutor(ToolRegistry((operation,)), RunStatePolicy()).execute(
            "update_task_status",
            {
                "items": [
                    {
                        "id": "test",
                        "text": "完成新任务",
                        "status": "completed",
                        "evidence": ["old-user-message"],
                    }
                ]
            },
        )
        self.assertFalse(result.success)

    def test_confirmation_is_bound_to_task_revision(self) -> None:
        operation = self.operation()
        self.execute(
            operation,
            {
                "items": [
                    {
                        "id": "test",
                        "text": "运行测试",
                        "status": "completed",
                        "evidence": ["listing"],
                    }
                ]
            },
        )
        board = operation.board
        for task_id, revision in [
            ("another-task", board["revision"]),
            (board["task_id"], board["revision"] - 1),
        ]:
            with self.assertRaises(ValueError):
                confirm_items(board, task_id=task_id, revision=revision, item_ids=("test",))
        confirmed = confirm_items(
            board, task_id=board["task_id"], revision=board["revision"], item_ids=("test",)
        )
        self.assertEqual(confirmed["items"][0]["status"], "completed")
        self.assertEqual(board["items"][0]["status"], "pending_confirmation")

    def test_projection_keeps_limits_and_does_not_modify_full_board(self) -> None:
        board = self.operation().board
        board["constraints"] = ["不修改文件"]
        board["criteria"] = ["展示测试结果"]
        board["facts"] = [
            {"text": "旧事实" * 100, "source": str(index), "confirmed": False}
            for index in range(12)
        ]
        board["results"] = [
            {"id": str(index), "tool": "read", "location": "长路径" * 200, "version": "v1"}
            for index in range(32)
        ]
        original = deepcopy(board)
        view = project_board(board, max_tokens=1000, count=len)
        self.assertEqual(view["constraints"], ["不修改文件"])
        self.assertIn("运行测试", json.dumps(view, ensure_ascii=False))
        self.assertLessEqual(len(json.dumps(view, ensure_ascii=False)), 1000)
        self.assertNotIn("results", view)
        self.assertNotIn("current_request", view)
        self.assertEqual(board, original)

    def test_old_state_cannot_keep_unverified_completion(self) -> None:
        board = self.operation().board
        legacy = json.loads(json.dumps(board))
        legacy["schema_version"] = 1
        legacy["items"][0]["status"] = "completed"
        restored = restore_board(legacy)
        assert restored is not None
        self.assertEqual(restored["schema_version"], 2)
        self.assertEqual(restored["items"][0]["status"], "pending_confirmation")

    def test_reused_call_id_with_changed_source_invalidates_confirmation(self) -> None:
        from langchain_core.messages import AIMessage, ToolMessage

        from workstation_agent.context.status import collect_results

        operation = self.operation()
        from langchain_core.messages import AnyMessage

        def messages(path: str) -> list[AnyMessage]:
            return [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "read_text_file",
                            "args": {"path": path},
                            "id": "read",
                            "type": "tool_call",
                        }
                    ],
                ),
                ToolMessage("相同正文", name="read_text_file", tool_call_id="read"),
            ]

        board = collect_results(operation.board, messages("old.txt"))
        board["items"] = [
            {"id": "item", "text": "检查 old.txt", "status": "completed", "evidence": ["read"]}
        ]
        board["facts"] = [{"text": "相同正文", "source": "read", "confirmed": True}]
        updated = collect_results(board, messages("new.txt"))
        self.assertEqual(updated["items"][0]["status"], "pending_confirmation")
        self.assertEqual(updated["facts"], [])
        self.assertGreater(updated["revision"], board["revision"])
