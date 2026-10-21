"""验证 PostgreSQL 会话重启后审批和状态验收仍然有效。"""

from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from langchain_core.messages import AIMessage, BaseMessage
from langgraph.types import Interrupt

from tests.test_agent import ScriptedModel
from tests.unit.test_context_management import call
from tests.unit.test_execution_control import CountingTool, EchoArguments
from workstation_agent.agent.factory import build_agent
from workstation_agent.application import AgentApplication, ToolApplication
from workstation_agent.bootstrap import MemoryApprovalPolicy, database_for
from workstation_agent.config import Settings
from workstation_agent.safety import AllowSafeReadsPolicy
from workstation_agent.storage.postgres.checkpoints import PostgresSessionBackend
from workstation_agent.storage.session_serializer import SessionSerializer
from workstation_agent.tools.checkpoints import AuthorizedCheckpointer
from workstation_agent.tools.executor import ToolExecutor
from workstation_agent.tools.langchain import local_tools
from workstation_agent.tools.registry import ToolRegistry


class PersistentResumeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        asyncio.get_running_loop().set_debug(False)
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = Settings(Path(self.temp.name))
        self.tool = CountingTool()

    def application(self, responses: list[BaseMessage]) -> AgentApplication:
        saver = AuthorizedCheckpointer(
            PostgresSessionBackend(database_for(self.settings)),
            self.settings.memory_access,
            MemoryApprovalPolicy(sessions=True),
        )
        tools = ToolApplication(ToolExecutor(ToolRegistry((self.tool,)), AllowSafeReadsPolicy()))
        graph = build_agent(
            ScriptedModel(responses=responses), local_tools(tools), checkpointer=saver
        )
        return AgentApplication(graph, checkpointer=saver)

    async def test_restarted_application_resumes_same_run_once(self) -> None:
        first_app = self.application([call("count", {"text": "执行一次"}, "one")])
        first = await first_app.run_task("执行操作", session_id="approval")
        self.assertEqual(first.status.value, "waiting_approval", first.error)
        restarted = self.application([AIMessage(content="完成")])
        pending = await restarted.pending_approvals("approval")
        self.assertEqual(len(pending), 1)
        self.assertEqual(next(iter(pending.values())).arguments, {"text": "执行一次"})
        result = await restarted.resume_task("approval", {key: "approve" for key in pending})
        self.assertEqual(result.status.value, "completed", result.error)
        self.assertEqual(result.id, first.id)
        self.assertEqual(self.tool.calls, ["执行一次"])
        self.assertEqual(await restarted.pending_approvals("approval"), {})

    async def test_user_confirms_saved_revision(self) -> None:
        app = self.application(
            [
                call("count", {}, "one"),
                call(
                    "update_task_status",
                    {
                        "items": [
                            {
                                "id": "item",
                                "text": "执行操作",
                                "status": "completed",
                                "evidence": ["one"],
                            }
                        ]
                    },
                    "status",
                ),
                AIMessage(content="请验收"),
            ]
        )
        run = await app.run_task("执行操作", session_id="review")
        self.assertEqual(run.status.value, "waiting_approval", run.error)
        pending = await app.pending_approvals("review")
        boards = []
        result = await app.resume_task(
            "review", {key: "approve" for key in pending}, on_status=boards.append
        )
        self.assertEqual(result.status.value, "completed", result.error)
        board = boards[-1]
        confirmed = await app.confirm_status(
            "review", task_id=board["task_id"], revision=board["revision"], item_ids=("item",)
        )
        self.assertEqual(confirmed["items"][0]["status"], "completed")
        with self.assertRaises(ValueError):
            await app.confirm_status(
                "review", task_id=board["task_id"], revision=board["revision"], item_ids=("item",)
            )

    def test_interrupt_parameters_are_redacted(self) -> None:
        serializer = SessionSerializer(("private-token",))
        original = Interrupt(value={"arguments": {"text": "private-token"}}, id="approval")
        restored = serializer.loads_typed(serializer.dumps_typed(original))
        self.assertNotIn("private-token", repr(restored))

    async def test_timeout_during_tool_persists_closed_run(self) -> None:
        from threading import Event

        from workstation_agent.safety import PermissionLevel
        from workstation_agent.tools.execution import check_execution

        stopped = Event()

        class BlockingTool(CountingTool):
            permission = PermissionLevel.SAFE_READ

            def run(self, arguments: EchoArguments) -> str:
                try:
                    while True:
                        check_execution()
                        stopped.wait(0.01)
                finally:
                    stopped.set()

        self.tool = BlockingTool()
        app = self.application([call("count", {}, "one")])
        app.run_timeout = 0.5
        result = await app.run_task("超时操作", session_id="timeout")
        self.assertEqual(result.status.value, "limit_reached", result.error)
        self.assertTrue(stopped.is_set())
        assert app.checkpointer is not None
        snapshot = await app.graph.aget_state(app.checkpointer.config("timeout"))
        self.assertTrue(snapshot.values["run_control"]["closed"])

    async def test_cancelled_persistent_run_cannot_resume(self) -> None:
        from threading import Event

        from workstation_agent.safety import PermissionLevel
        from workstation_agent.tools.execution import check_execution

        started = Event()
        stopped = Event()

        class BlockingTool(CountingTool):
            permission = PermissionLevel.SAFE_READ

            def run(self, arguments: EchoArguments) -> str:
                started.set()
                try:
                    while True:
                        check_execution()
                        stopped.wait(0.01)
                finally:
                    stopped.set()

        self.tool = BlockingTool()
        app = self.application([call("count", {}, "one")])
        boards = []
        task = asyncio.create_task(
            app.run_task("取消操作", session_id="cancel", on_status=boards.append)
        )
        await asyncio.to_thread(started.wait, 10)
        self.assertTrue(started.is_set())
        self.assertTrue(app.cancel(boards[-1]["task_id"]))
        self.assertFalse(app.cancel(boards[-1]["task_id"]))
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(stopped.is_set())
        self.assertEqual(await app.pending_approvals("cancel"), {})
