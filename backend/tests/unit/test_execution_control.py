"""验证审批范围、恢复、并行副作用及运行停止边界。"""

from __future__ import annotations

import asyncio
import unittest
from threading import Event
from time import time
from unittest.mock import patch

from langchain_core.messages import AIMessage, BaseMessage

from tests.test_agent import ScriptedModel
from tests.unit.test_context_management import call
from workstation_agent.agent.factory import build_agent
from workstation_agent.application import AgentApplication, ToolApplication
from workstation_agent.domain.execution import ExecutionControl
from workstation_agent.safety import AllowSafeReadsPolicy, PermissionLevel
from workstation_agent.tools.contracts import PydanticTool, ToolArguments
from workstation_agent.tools.execution import EXECUTION, ToolGrant, approved_call, check_execution
from workstation_agent.tools.executor import ToolExecutor
from workstation_agent.tools.langchain import local_tools
from workstation_agent.tools.registry import ToolRegistry


class EchoArguments(ToolArguments):
    text: str = "hello"


class CountingTool(PydanticTool[EchoArguments]):
    """记录实际副作用次数，不通过模型声明判断是否执行。"""

    name = "count"
    description = "计数工具"
    permission = PermissionLevel.SENSITIVE
    arguments_model = EchoArguments

    def __init__(self) -> None:
        self.calls: list[str] = []

    def run(self, arguments: EchoArguments) -> str:
        self.calls.append(arguments.text)
        return arguments.text


class ExecutionControlTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        asyncio.get_running_loop().set_debug(False)

    def application(
        self, tool: CountingTool, responses: list[BaseMessage], *, run_timeout: float = 300.0
    ) -> AgentApplication:
        tools = ToolApplication(ToolExecutor(ToolRegistry((tool,)), AllowSafeReadsPolicy()))
        return AgentApplication(
            build_agent(ScriptedModel(responses=responses), local_tools(tools)),
            run_timeout=run_timeout,
        )

    async def test_resume_preserves_run_and_grants_only_one_call(self) -> None:
        tool = CountingTool()
        app = self.application(
            tool,
            [
                call("count", {"text": "one"}, "one"),
                call("count", {"text": "two"}, "two"),
                AIMessage(content="结束"),
            ],
        )
        first = await app.run_task("执行两个操作")
        self.assertEqual(first.status.value, "waiting_approval", first.error)
        self.assertEqual(tool.calls, [])
        pending = await app.pending_approvals(first.session_id)
        with self.assertRaises(ValueError):
            await app.resume_task(first.session_id, {"stale": "approve"})
        second = await app.resume_task(first.session_id, {key: "approve" for key in pending})
        self.assertEqual(second.id, first.id)
        self.assertEqual(second.status.value, "waiting_approval", second.error)
        self.assertEqual(tool.calls, ["one"])
        pending2 = await app.pending_approvals(first.session_id)
        third = await app.resume_task(first.session_id, {key: "reject" for key in pending2})
        self.assertEqual(third.status.value, "completed", third.error)
        self.assertEqual(tool.calls, ["one"])
        with self.assertRaises(ValueError):
            await app.resume_task(first.session_id, {key: "approve" for key in pending2})

    async def test_parallel_approved_calls_execute_once(self) -> None:
        tool = CountingTool()
        parallel = AIMessage(
            content="",
            tool_calls=[
                {"name": "count", "args": {"text": name}, "id": name, "type": "tool_call"}
                for name in ("first", "second")
            ],
        )
        app = self.application(tool, [parallel, AIMessage(content="完成")])
        run = await app.run_task("并行操作")
        pending = await app.pending_approvals(run.session_id)
        self.assertEqual(len(pending), 2)
        result = await app.resume_task(run.session_id, {key: "approve" for key in pending})
        self.assertEqual(result.status.value, "completed", result.error)
        self.assertCountEqual(tool.calls, ["first", "second"])

    async def test_expired_approval_cannot_execute(self) -> None:
        tool = CountingTool()
        app = self.application(tool, [call("count", {}, "one"), AIMessage(content="完成")])
        run = await app.run_task("等待审批")
        pending = await app.pending_approvals(run.session_id)
        with patch("workstation_agent.application.service.time", return_value=time() + 400):
            result = await app.resume_task(run.session_id, {key: "approve" for key in pending})
        self.assertEqual(result.status.value, "limit_reached", result.error)
        self.assertEqual(tool.calls, [])
        self.assertEqual(await app.pending_approvals(run.session_id), {})

    async def test_timeout_and_cancel_stop_cooperative_worker(self) -> None:
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

        tool = BlockingTool()
        app = self.application(tool, [call("count", {}, "one")], run_timeout=2)
        result = await app.run_task("超时操作")
        self.assertEqual(result.status.value, "limit_reached", result.error)
        self.assertTrue(stopped.is_set())
        started.clear()
        stopped.clear()
        app = self.application(tool, [call("count", {}, "one")], run_timeout=100)
        run_ids: list[str] = []
        task = asyncio.create_task(app.run_task("取消操作", on_started=run_ids.append))
        await asyncio.to_thread(started.wait, 20)
        self.assertTrue(started.is_set())
        self.assertTrue(app.cancel(run_ids[-1]))
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(stopped.is_set())
        self.assertFalse(app.cancel(run_ids[-1]))

    def test_grant_cannot_change_arguments_and_does_not_escape_scope(self) -> None:
        tool = CountingTool()
        executor = ToolExecutor(ToolRegistry((tool,)), AllowSafeReadsPolicy())
        from workstation_agent.safety import PermissionRequired

        with approved_call(ToolGrant.for_call("count", {"text": "one"})):
            self.assertTrue(executor.execute("count", {"text": "one"}).success)
            with self.assertRaises(PermissionRequired):
                executor.execute("count", {"text": "two"})
        with self.assertRaises(PermissionRequired):
            executor.execute("count", {"text": "one"})

    def test_cancelled_control_refuses_new_side_effect(self) -> None:
        tool = CountingTool()
        control = ExecutionControl(time() + 10)
        control.cancelled.set()
        token = EXECUTION.set(control)
        try:
            result = ToolExecutor(ToolRegistry((tool,)), AllowSafeReadsPolicy()).execute(
                "count", {}
            )
            self.assertFalse(result.success)
            self.assertEqual(tool.calls, [])
        finally:
            EXECUTION.reset(token)

    async def test_completed_parallel_operation_is_not_replayed(self) -> None:
        class AllowedTool(CountingTool):
            name = "allowed"
            permission = PermissionLevel.SAFE_READ

        allowed = AllowedTool()
        protected = CountingTool()
        tools = ToolApplication(
            ToolExecutor(ToolRegistry((allowed, protected)), AllowSafeReadsPolicy())
        )
        parallel = AIMessage(
            content="",
            tool_calls=[
                {"name": "allowed", "args": {}, "id": "allowed", "type": "tool_call"},
                {"name": "count", "args": {}, "id": "protected", "type": "tool_call"},
            ],
        )
        app = AgentApplication(
            build_agent(
                ScriptedModel(responses=[parallel, AIMessage(content="完成")]), local_tools(tools)
            )
        )
        first = await app.run_task("并行执行")
        self.assertEqual(len(allowed.calls), 1)
        pending = await app.pending_approvals(first.session_id)
        result = await app.resume_task(first.session_id, {key: "approve" for key in pending})
        self.assertEqual(result.status.value, "completed", result.error)
        self.assertEqual(len(allowed.calls), 1, "恢复时不能重复执行已经成功的工具")

    async def test_approval_does_not_reset_model_call_limit(self) -> None:
        tool = CountingTool()
        tools = ToolApplication(ToolExecutor(ToolRegistry((tool,)), AllowSafeReadsPolicy()))
        app = AgentApplication(
            build_agent(
                ScriptedModel(responses=[call("count", {}, "one"), AIMessage(content="不应调用")]),
                local_tools(tools),
                max_model_calls=1,
            )
        )
        first = await app.run_task("执行操作")
        pending = await app.pending_approvals(first.session_id)
        result = await app.resume_task(first.session_id, {key: "approve" for key in pending})
        self.assertEqual(result.status.value, "limit_reached", result.error)
        self.assertEqual(tool.calls, ["hello"])

    async def test_large_saved_status_is_projected_before_model_call(self) -> None:
        from tests.unit.test_context_management import CapturingModel
        from workstation_agent.context.config import ContextSettings
        from workstation_agent.context.state import ContextRuntime
        from workstation_agent.context.status import initialize

        board = initialize([], None, ContextRuntime(run_id="task", task="检查项目"))
        board["facts"] = [
            {"text": "旧事实" * 100, "source": str(i), "confirmed": False} for i in range(12)
        ]
        board["results"] = [
            {"id": str(i), "tool": "read", "location": "长路径" * 200, "version": "v1"}
            for i in range(32)
        ]
        model = CapturingModel(responses=[AIMessage(content="完成")])
        graph = build_agent(model, [], context_settings=ContextSettings(window=9000))
        from langchain_core.runnables import RunnableConfig

        config: RunnableConfig = {"configurable": {"thread_id": "projection"}}
        await graph.aupdate_state(config, {"task_board": board}, as_node="tools")
        # 应用内临时会话不接受外部持久会话名；直接使用同一配置验证框架请求边界。
        from langchain_core.messages import HumanMessage

        runtime = ContextRuntime(task="继续检查")
        await graph.ainvoke(
            {"messages": [HumanMessage("继续检查")]}, config=config, context=runtime
        )
        self.assertEqual(len(model.requests), 1)
        self.assertIn("projected_task_board", runtime.usages[-1]["reasons"])
        self.assertLess(runtime.usages[-1]["data_tokens"], 1800)
        snapshot = await graph.aget_state(config)
        self.assertEqual(len(snapshot.values["task_board"]["results"]), 32)
        self.assertEqual(len(snapshot.values["task_board"]["facts"]), 12)
