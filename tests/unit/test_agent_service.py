"""验证常驻任务用例的容量、关闭和异常边界，不访问文件或外部服务。"""

from __future__ import annotations

import asyncio
import unittest
from contextlib import AbstractContextManager, nullcontext

from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.runnables import RunnableConfig

from tests.test_agent import ScriptedModel, call_message
from tests.unit.test_execution_control import CountingTool
from workstation_agent.agent.factory import build_agent
from workstation_agent.application import AgentApplication, ToolApplication
from workstation_agent.application.agent_service import AgentServiceApplication, AgentServiceError
from workstation_agent.domain.runs import RunStatus
from workstation_agent.safety import AllowSafeReadsPolicy
from workstation_agent.tools.executor import ToolExecutor
from workstation_agent.tools.langchain import local_tools
from workstation_agent.tools.registry import ToolRegistry


class InMemorySessions:
    """为单事件循环测试提供会话配置，执行状态由官方内存检查点保存。"""

    def config(self, session_id: str) -> RunnableConfig:
        return {"configurable": {"thread_id": session_id}}

    def lease(self, config: RunnableConfig, *, deleting: bool = False) -> AbstractContextManager[None]:
        return nullcontext()


class AgentServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        asyncio.get_running_loop().set_debug(False)

    def application(self, responses: list[BaseMessage]) -> AgentServiceApplication:
        """组合真实 Agent 和工具边界，只替换模型回复与会话锁。"""
        tools = ToolApplication(ToolExecutor(ToolRegistry((CountingTool(),)), AllowSafeReadsPolicy()))
        application = AgentApplication(
            build_agent(ScriptedModel(responses=responses), local_tools(tools)),
            checkpointer=InMemorySessions(),
        )
        manager = AgentServiceApplication(application, capacity=1)
        self.addAsyncCleanup(manager.close)
        return manager

    async def wait_status(self, manager: AgentServiceApplication, run_id: str, expected: RunStatus) -> None:
        async with asyncio.timeout(5):
            while manager.get(run_id).status != expected:
                await asyncio.sleep(0.01)

    async def test_capacity_evicts_finished_records_and_returns_copies(self) -> None:
        manager = self.application([AIMessage(content="完成")])
        first = await manager.submit("第一项")
        await self.wait_status(manager, first.id, RunStatus.COMPLETED)
        first.answer = "篡改副本"
        self.assertEqual(manager.get(first.id).answer, "完成")
        second = await manager.submit("第二项")
        await self.wait_status(manager, second.id, RunStatus.COMPLETED)
        with self.assertRaises(AgentServiceError) as caught:
            manager.get(first.id)
        self.assertEqual(caught.exception.status, 404)
        self.assertEqual(len(manager.list_runs()), 1)

    async def test_full_capacity_preserves_approval_and_still_allows_resume(self) -> None:
        manager = self.application([call_message("count", {}), AIMessage(content="完成")])
        run = await manager.submit("审批任务", session_id="approval")
        await self.wait_status(manager, run.id, RunStatus.WAITING_APPROVAL)
        with self.assertRaises(AgentServiceError) as caught:
            await manager.submit("新任务")
        self.assertEqual(caught.exception.status, 503)
        pending = await manager.approvals("approval")
        resumed = await manager.resume("approval", {key: "reject" for key in pending})
        self.assertEqual(resumed.id, run.id)
        await self.wait_status(manager, run.id, RunStatus.COMPLETED)

    async def test_closed_service_rejects_submission(self) -> None:
        manager = self.application([AIMessage(content="完成")])
        await manager.close()
        with self.assertRaises(AgentServiceError) as caught:
            await manager.submit("新任务")
        self.assertEqual(caught.exception.code, "service_stopping")

    async def test_invalid_resume_returns_safe_error(self) -> None:
        manager = self.application([AIMessage(content="完成")])
        with self.assertRaises(AgentServiceError) as caught:
            await manager.resume("missing", {"private-id": "approve"})
        self.assertEqual(caught.exception.status, 409)
        self.assertNotIn("private-id", str(caught.exception))

    async def test_cancelled_submission_does_not_abandon_accepted_work(self) -> None:
        manager = self.application([AIMessage(content="完成")])
        submission = asyncio.create_task(manager.submit("客户端断开"))
        await asyncio.sleep(0)
        submission.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await submission
        async with asyncio.timeout(5):
            while not manager.list_runs():
                await asyncio.sleep(0.01)
        run = manager.list_runs()[0]
        await self.wait_status(manager, run.id, RunStatus.COMPLETED)
