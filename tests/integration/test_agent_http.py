"""验证 HTTP 入口复用官方 Agent、SQLite 审批和受控工具执行链。"""

from __future__ import annotations

import time
import unittest
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event

from langchain_core.messages import AIMessage, BaseMessage
from pydantic import TypeAdapter
from starlette.testclient import TestClient

from tests.test_agent import ScriptedModel, call_message
from tests.unit.test_execution_control import CountingTool, EchoArguments
from workstation_agent.agent.factory import build_agent
from workstation_agent.application import AgentApplication, ToolApplication
from workstation_agent.application.agent_service import ServiceRun
from workstation_agent.bootstrap import MemoryApprovalPolicy
from workstation_agent.config import Settings
from workstation_agent.domain.runs import RunStatus
from workstation_agent.interfaces.agent_http import build_agent_service
from workstation_agent.safety import AllowSafeReadsPolicy, PermissionLevel
from workstation_agent.storage.sqlite.sessions import SQLiteSessionBackend
from workstation_agent.tools.checkpoints import AuthorizedCheckpointer
from workstation_agent.tools.execution import check_execution
from workstation_agent.tools.executor import ToolExecutor
from workstation_agent.tools.langchain import local_tools
from workstation_agent.tools.registry import ToolRegistry

_RUN = TypeAdapter(ServiceRun)


class BlockingReadTool(CountingTool):
    """在真实工具工作线程中等待取消，便于检查忙碌与退出时的资源回收。"""

    permission = PermissionLevel.SAFE_READ

    def __init__(self) -> None:
        super().__init__()
        self.started = Event()
        self.stopped = Event()

    def run(self, arguments: EchoArguments) -> str:
        self.started.set()
        try:
            while True:
                check_execution()
                self.stopped.wait(0.01)
        finally:
            self.stopped.set()


class AgentHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.settings = Settings(
            Path(self.directory.name), agent_service_token="agent_http_test_" + "x" * 32
        )
        self.tool: CountingTool = CountingTool()

    def client(self, responses: list[BaseMessage], *, timeout: float = 300) -> TestClient:
        """每次启动独立应用实例，但会话使用相同的真实 SQLite 路径。"""
        @asynccontextmanager
        async def application() -> AsyncIterator[AgentApplication]:
            checkpointer = AuthorizedCheckpointer(
                SQLiteSessionBackend(
                    self.settings.session_database_path, self.settings.workspace,
                    secrets=(self.settings.agent_service_token,),
                ), self.settings.memory_access, MemoryApprovalPolicy(sessions=True),
            )
            tools = ToolApplication(ToolExecutor(ToolRegistry((self.tool,)), AllowSafeReadsPolicy()))
            yield AgentApplication(
                build_agent(ScriptedModel(responses=responses), local_tools(tools), checkpointer=checkpointer),
                checkpointer=checkpointer, run_timeout=timeout,
            )

        return TestClient(
            build_agent_service(self.settings, application_factory=application),
            base_url=self.settings.agent_service_url,
            headers={"Authorization": f"Bearer {self.settings.agent_service_token}"},
        )

    def wait_run(self, client: TestClient, run_id: str, status: RunStatus) -> ServiceRun:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            response = client.get(f"/runs/{run_id}")
            self.assertEqual(response.status_code, 200, response.text)
            run = _RUN.validate_json(response.content)
            if run.status == status:
                return run
            time.sleep(0.01)
        self.fail(f"任务未进入预期状态：{status}")

    def test_submit_poll_and_list_return_real_run(self) -> None:
        with self.client([AIMessage(content="完成")]) as client:
            response = client.post("/runs", json={"task": "回答问题", "session_id": "first"})
            self.assertEqual(response.status_code, 202, response.text)
            run = _RUN.validate_json(response.content)
            completed = self.wait_run(client, run.id, RunStatus.COMPLETED)
            self.assertEqual(completed.answer, "完成")
            self.assertEqual(completed.session_id, "first")
            self.assertTrue(completed.steps)
            self.assertEqual(client.get("/runs").json()[0]["id"], run.id)
            self.assertFalse(client.post(f"/runs/{run.id}/cancel").json()["cancel_requested"])
            self.assertEqual(client.get("/runs/missing").status_code, 404)

    def test_builder_rejects_missing_credentials_and_conflicting_ports(self) -> None:
        with self.assertRaises(ValueError):
            build_agent_service(replace(self.settings, agent_service_token=None))
        with self.assertRaises(ValueError):
            build_agent_service(replace(self.settings, agent_service_port=self.settings.service_port))

    def test_authentication_host_origin_and_schema_are_checked(self) -> None:
        with self.client([AIMessage(content="完成")]) as client:
            self.assertEqual(client.get("/health", headers={"Authorization": ""}).status_code, 200)
            self.assertEqual(client.post("/runs", content="{", headers={"Authorization": "wrong"}).status_code, 401)
            self.assertEqual(client.get("/runs", headers={"Host": "evil.example"}).status_code, 421)
            self.assertEqual(client.get("/runs", headers={"Origin": "https://evil.example"}).status_code, 403)
            for body in ({"task": " "}, {"task": 1}, {"task": "a", "workspace": "outside"},
                         {"task": "a", "session_id": "../bad"}):
                with self.subTest(body=body):
                    self.assertEqual(client.post("/runs", json=body).status_code, 422)
            malformed = client.post("/runs", content="{", headers={"Content-Type": "application/json"})
            self.assertEqual(malformed.status_code, 422)
            self.assertEqual(client.post("/runs", content="text").status_code, 415)
            oversized = client.post("/runs", content="x" * 65_537, headers={"Content-Type": "application/json"})
            self.assertEqual(oversized.status_code, 413)
            self.assertEqual(client.get("/runs").json(), [])

    def test_restarted_service_resumes_approval_with_original_id_once(self) -> None:
        with self.client([call_message("count", {"text": "审批内容"})]) as client:
            response = client.post("/runs", json={"task": "执行操作", "session_id": "approval"})
            run = _RUN.validate_json(response.content)
            self.wait_run(client, run.id, RunStatus.WAITING_APPROVAL)
            self.assertEqual(self.tool.calls, [])
            self.assertEqual(client.post("/runs", json={"task": "覆盖任务", "session_id": "approval"}).status_code, 409)
        with self.client([AIMessage(content="完成")]) as client:
            self.assertEqual(client.get(f"/runs/{run.id}").status_code, 404)
            pending = client.get("/sessions/approval/approvals").json()
            self.assertEqual(len(pending), 1)
            bad = client.post("/sessions/approval/resume", json={"decisions": {"old-id": "approve"}})
            self.assertEqual(bad.status_code, 409)
            decisions = {key: "approve" for key in pending}
            resumed = client.post("/sessions/approval/resume", json={"decisions": decisions})
            self.assertEqual(resumed.status_code, 202, resumed.text)
            self.assertEqual(_RUN.validate_json(resumed.content).id, run.id)
            self.wait_run(client, run.id, RunStatus.COMPLETED)
            self.assertEqual(self.tool.calls, ["审批内容"])
            self.assertEqual(client.get("/sessions/approval/approvals").json(), {})
            self.assertEqual(client.post("/sessions/approval/resume", json={"decisions": decisions}).status_code, 409)

    def test_rejection_does_not_execute_tool(self) -> None:
        with self.client([call_message("count", {}), AIMessage(content="已拒绝")]) as client:
            run = _RUN.validate_json(client.post("/runs", json={"task": "操作", "session_id": "reject"}).content)
            self.wait_run(client, run.id, RunStatus.WAITING_APPROVAL)
            pending = client.get("/sessions/reject/approvals").json()
            response = client.post("/sessions/reject/resume", json={"decisions": {key: "reject" for key in pending}})
            self.assertEqual(response.status_code, 202)
            self.wait_run(client, run.id, RunStatus.COMPLETED)
            self.assertEqual(self.tool.calls, [])

    def test_busy_cancel_and_shutdown_wait_for_tool_exit(self) -> None:
        tool = BlockingReadTool()
        self.tool = tool
        with self.client([call_message("count", {})]) as client:
            run = _RUN.validate_json(client.post("/runs", json={"task": "阻塞操作"}).content)
            self.assertTrue(tool.started.wait(5))
            self.assertEqual(client.post("/runs", json={"task": "第二个任务"}).status_code, 409)
            self.assertEqual(client.get(f"/sessions/{run.session_id}/approvals").status_code, 409)
            self.assertEqual(client.post(f"/runs/{run.id}/cancel").status_code, 202)
            self.wait_run(client, run.id, RunStatus.CANCELLED)
            self.assertTrue(tool.stopped.is_set())
        tool = BlockingReadTool()
        self.tool = tool
        with self.client([call_message("count", {})]) as client:
            self.assertEqual(client.post("/runs", json={"task": "退出时取消"}).status_code, 202)
            self.assertTrue(tool.started.wait(5))
        self.assertTrue(tool.stopped.is_set())

    def test_service_timeout_and_known_secret_redaction(self) -> None:
        tool = BlockingReadTool()
        self.tool = tool
        with self.client([call_message("count", {})], timeout=0.5) as client:
            run = _RUN.validate_json(client.post("/runs", json={"task": "超时操作"}).content)
            result = self.wait_run(client, run.id, RunStatus.LIMIT_REACHED)
            self.assertTrue(tool.started.is_set())
            self.assertTrue(tool.stopped.is_set())
            self.assertIn("时间限制", result.error or "")
        with self.client([AIMessage(content=f"返回 {self.settings.agent_service_token}")]) as client:
            run = _RUN.validate_json(client.post("/runs", json={"task": "答复"}).content)
            completed = self.wait_run(client, run.id, RunStatus.COMPLETED)
            self.assertNotIn(str(self.settings.agent_service_token), str(completed.answer))


if __name__ == "__main__":
    unittest.main()
