"""验证 Sonar 指出的契约缺口和重构时必须保留的运行行为。"""

import unittest
from unittest.mock import patch

from workstation_agent.rag.index import terms
from workstation_agent.safety import AllowSafeReadsPolicy, PermissionLevel
from workstation_agent.tools.contracts import ToolError
from workstation_agent.tools.effects import authorized_operation
from workstation_agent.tools.executor import ToolResult


class SonarRegressionTests(unittest.TestCase):
    """验证授权回调、检索和服务退出行为，防止静态分析重构改变原有契约。"""

    def test_success_without_callback_result_is_a_controlled_error(self) -> None:
        policy = AllowSafeReadsPolicy()
        with (
            patch(
                "workstation_agent.tools.effects.ToolExecutor.execute",
                return_value=ToolResult("internal", "ok"),
            ),
            self.assertRaises(ToolError) as caught,
        ):
            authorized_operation("internal", PermissionLevel.SAFE_READ, policy, lambda: 1)
        self.assertEqual(caught.exception.code, "invalid_output")

    def test_ascii_identifiers_and_chinese_terms_keep_existing_boundaries(self) -> None:
        self.assertEqual(
            terms("café 汉字 foo.bar_1"),
            ["caf", "汉", "字", "汉字", "foo.bar_1", "foo", "bar", "1"],
        )

    def test_callback_can_return_none(self) -> None:
        result = authorized_operation(
            "internal", PermissionLevel.SAFE_READ, AllowSafeReadsPolicy(), lambda: None
        )
        self.assertIsNone(result)

    def test_context_profile_resolution_preserves_settings_type(self) -> None:
        from langchain_core.messages import AIMessage

        from tests.test_agent import ScriptedModel
        from workstation_agent.context.budget import resolve_settings
        from workstation_agent.context.config import ContextSettings

        settings = ContextSettings(board_tokens=777)
        resolved = resolve_settings(settings, ScriptedModel(responses=[AIMessage(content="完成")]))
        self.assertIsInstance(resolved, ContextSettings)
        self.assertEqual(resolved.board_tokens, 777)

    def test_service_exit_signals_are_not_swallowed(self) -> None:
        from pathlib import Path

        from workstation_agent.application import ToolApplication
        from workstation_agent.config import Settings
        from workstation_agent.interfaces.mcp_server import run_local_service
        from workstation_agent.tools import ToolExecutor, ToolRegistry

        application = ToolApplication(ToolExecutor(ToolRegistry(), AllowSafeReadsPolicy()))
        settings = Settings(Path.cwd())
        for code in (0, 1):
            with (
                self.subTest(code=code),
                patch(
                    "workstation_agent.interfaces.mcp_server.build_local_service",
                    return_value=object(),
                ),
                patch(
                    "workstation_agent.interfaces.mcp_server.uvicorn.run",
                    side_effect=SystemExit(code),
                ),
                self.assertRaises(SystemExit) as caught,
            ):
                run_local_service(application, settings)
            self.assertEqual(caught.exception.code, code)

    def test_summary_removal_is_a_valid_framework_update(self) -> None:
        from langchain_core.messages import RemoveMessage

        from workstation_agent.application.service import AgentApplication
        from workstation_agent.domain.runs import Run

        run = Run(task="测试", session_id="test")
        latest = AgentApplication._record_messages(
            "ContextMiddleware.before_model",
            {"messages": [RemoveMessage(id="__remove_all__")]},
            run,
        )
        self.assertIsNone(latest)
        self.assertEqual(run.steps, [])

    def test_cli_normalizes_service_failure_and_preserves_exit(self) -> None:
        from pathlib import Path

        from workstation_agent.cli import main
        from workstation_agent.config import Settings

        settings = Settings(Path.cwd(), service_token="x" * 32)
        with (
            patch("workstation_agent.cli.Settings.from_env", return_value=settings),
            patch("workstation_agent.cli.build_tool_application", return_value=object()),
            patch(
                "workstation_agent.interfaces.mcp_server.run_local_service",
                side_effect=SystemExit(3),
            ),
            self.assertRaises(SystemExit) as caught,
        ):
            main(["serve"])
        self.assertEqual(caught.exception.code, 1)


class CancellationCleanupTests(unittest.IsolatedAsyncioTestCase):
    """确认重复取消不会让工具线程跳过清理，也不会吞掉调用方的取消异常。"""

    async def test_repeated_cancellation_waits_for_worker_and_propagates(self) -> None:
        import asyncio
        from threading import Event
        from time import time

        from workstation_agent.domain.execution import ExecutionControl
        from workstation_agent.tools.execution import EXECUTION, execute_blocking

        started, cleaning, release, finished = Event(), Event(), Event(), Event()
        control = ExecutionControl(time() + 10)

        def worker() -> None:
            started.set()
            try:
                control.cancelled.wait(5)
            finally:
                cleaning.set()
                release.wait(5)
                finished.set()

        token = EXECUTION.set(control)
        try:
            task = asyncio.create_task(execute_blocking(worker))
            await asyncio.to_thread(started.wait, 5)
            task.cancel()
            await asyncio.to_thread(cleaning.wait, 5)
            task.cancel()
            await asyncio.sleep(0)
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(finished.is_set())
        finally:
            release.set()
            EXECUTION.reset(token)
