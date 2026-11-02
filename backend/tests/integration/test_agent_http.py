"""验证 HTTP 入口复用官方 Agent、PostgreSQL 审批和受控工具执行链。"""

from __future__ import annotations

import time
import unittest
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from unittest.mock import patch

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from pydantic import TypeAdapter
from starlette.testclient import TestClient

from tests.test_agent import ScriptedModel, call_message
from tests.unit.test_execution_control import CountingTool, EchoArguments
from workstation_agent.agent.factory import build_agent
from workstation_agent.application import AgentApplication, ToolApplication
from workstation_agent.application.agent_service import ServiceRun
from workstation_agent.bootstrap import MemoryApprovalPolicy, database_for, build_run_application, build_rag_application, build_tool_application
from workstation_agent.config import Settings
from workstation_agent.domain.runs import Run, RunStatus, Step, StepKind
from workstation_agent.storage.postgres.runs import PostgresRunRepository
from workstation_agent.tools.contracts import ToolError
from workstation_agent.interfaces.agent_http import build_agent_service
from workstation_agent.safety import AllowSafeReadsPolicy, PermissionLevel
from workstation_agent.storage.postgres.checkpoints import PostgresSessionBackend
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
        """每次启动独立应用实例，但会话使用同一 PostgreSQL 数据库和工作区分区。"""
        @asynccontextmanager
        async def application() -> AsyncIterator[AgentApplication]:
            checkpointer = AuthorizedCheckpointer(
                PostgresSessionBackend(
                    database_for(self.settings),
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
            self.assertEqual(client.get("/runs").json()["items"][0]["id"], run.id)
            self.assertFalse(client.post(f"/runs/{run.id}/cancel").json()["cancel_requested"])
            self.assertEqual(client.get("/runs/missing").status_code, 404)

    def test_history_survives_restart_and_explicit_delete_keeps_messages(self) -> None:
        with self.client([AIMessage(content="第一次完成"), AIMessage(content="第二次完成")]) as client:
            first = client.post("/runs", json={"task": "第一项", "session_id": "history"}).json()
            self.wait_run(client, first["id"], RunStatus.COMPLETED)
            second = client.post("/runs", json={"task": "第二项", "session_id": "history"}).json()
            self.wait_run(client, second["id"], RunStatus.COMPLETED)
        with self.client([AIMessage(content="第三次完成")]) as client:
            saved = client.get(f'/runs/{first["id"]}').json()
            self.assertTrue(saved["steps"])
            self.assertEqual(saved["answer"], "第一次完成")
            page = client.get("/runs?session_id=history&limit=1").json()
            self.assertEqual(page["items"][0]["id"], second["id"])
            self.assertEqual(page["next_offset"], 1)
            self.assertEqual(client.get("/runs?session_id=history&limit=1&offset=1").json()["items"][0]["id"], first["id"])
            for query in ("limit=0", "offset=-1", "session_id=../bad", "unknown=1"):
                self.assertEqual(client.get("/runs?" + query).status_code, 422)
            self.assertEqual(client.delete(f'/runs/{first["id"]}').status_code, 200)
            self.assertEqual(client.get(f'/runs/{first["id"]}').status_code, 404)
            self.assertEqual(len(client.get("/sessions/history/messages").json()["items"]), 4)
            third = client.post("/runs", json={"task": "继续提问", "session_id": "history"}).json()
            self.wait_run(client, third["id"], RunStatus.COMPLETED)
            revision = client.get("/sessions/history").json()["revision"]
            self.assertEqual(client.delete(f"/sessions/history?revision={revision}").status_code, 200)
            self.assertEqual(client.get("/runs?session_id=history").json()["items"], [])
            self.assertEqual(client.get(f'/runs/{third["id"]}').status_code, 404)

    def test_restart_marks_orphan_interrupted_and_keeps_last_steps(self) -> None:
        run = Run(task="宕机前任务", session_id="orphan")
        step = Step(run_id=run.id, kind=StepKind.MODEL, name="model")
        step.finish()
        run.steps.append(step)
        run.transition(RunStatus.THINKING)
        build_run_application(self.settings).save(run)
        with self.client([AIMessage(content="新任务完成")]) as client:
            restored = self.wait_run(client, run.id, RunStatus.INTERRUPTED)
            self.assertEqual(restored.steps[0].id, step.id)
            self.assertEqual(client.post(f"/runs/{run.id}/cancel").status_code, 200)
            new = client.post("/runs", json={"task": "重新安排任务", "session_id": "orphan"}).json()
            self.wait_run(client, new["id"], RunStatus.COMPLETED)

    def test_other_service_cannot_interrupt_running_session(self) -> None:
        tool = BlockingReadTool()
        self.tool = tool
        with self.client([call_message("count", {})]) as first:
            run = first.post("/runs", json={"task": "正在执行", "session_id": "live"}).json()
            self.assertTrue(tool.started.wait(5))
            with self.client([AIMessage(content="备用服务")]) as second:
                detail = second.get(f'/runs/{run["id"]}').json()
                self.assertIn(detail["status"], {"thinking", "acting", "observing"})
                self.assertTrue(any(step["kind"] == "model" for step in detail["steps"]))
                self.assertEqual(second.delete(f'/runs/{run["id"]}').status_code, 409)
            self.assertEqual(first.post(f'/runs/{run["id"]}/cancel').status_code, 202)
            self.wait_run(first, run["id"], RunStatus.CANCELLED)

    def test_restart_closes_abandoned_checkpoint_without_replaying_tools(self) -> None:
        run = Run(task="未完成的原任务", session_id="abandoned")
        run.transition(RunStatus.ACTING)
        build_run_application(self.settings).save(run)
        saver = AuthorizedCheckpointer(PostgresSessionBackend(database_for(self.settings)),
                                       self.settings.memory_access, MemoryApprovalPolicy(sessions=True))
        graph = build_agent(ScriptedModel(responses=[AIMessage(content="不应执行")]), [], checkpointer=saver)
        graph.update_state(saver.config(run.session_id), {
            "messages": [HumanMessage(content=run.task, id=run.id + "_user")],
            "run_control": {"schema_version": 1, "run_id": run.id, "task": run.task,
                            "deadline": time.time() + 60, "closed": False},
        }, as_node="model")
        with self.client([AIMessage(content="新任务已完成")]) as client:
            self.wait_run(client, run.id, RunStatus.INTERRUPTED)
            self.assertEqual(self.tool.calls, [])
            saved = saver.get_tuple(saver.config(run.session_id))
            assert saved is not None
            self.assertTrue(saved.checkpoint["channel_values"]["run_control"]["closed"])
            next_run = client.post("/runs", json={"task": "用户重新发出的任务", "session_id": run.session_id}).json()
            self.wait_run(client, next_run["id"], RunStatus.COMPLETED)

    def test_initial_save_failure_does_not_accept_or_execute_task(self) -> None:
        with self.client([call_message("count", {})]) as client:
            with patch.object(PostgresRunRepository, "save", side_effect=ToolError("private-database-error", code="database_failed")):
                result = client.post("/runs", json={"task": "不应执行", "session_id": "save_failed"})
            self.assertEqual(result.status_code, 503)
            self.assertNotIn("private-database-error", result.text)
            self.assertEqual(self.tool.calls, [])
            self.assertEqual(client.get("/runs").json()["items"], [])
            self.assertEqual(client.get("/sessions").json()["items"], [])

    def test_step_save_failure_stops_before_next_tool(self) -> None:
        tool = CountingTool()
        tool.permission = PermissionLevel.SAFE_READ
        self.tool = tool
        save = PostgresRunRepository.save

        def fail_after_model(repository: PostgresRunRepository, run: Run) -> None:
            if any(step.kind == StepKind.MODEL for step in run.steps):
                raise ToolError("private-step-error", code="database_failed")
            save(repository, run)

        with self.client([call_message("count", {}), AIMessage(content="不应继续")]) as client:
            with patch.object(PostgresRunRepository, "save", fail_after_model):
                result = client.post("/runs", json={"task": "保存失败应停止", "session_id": "step_failed"})
                self.assertEqual(result.status_code, 202)
                failed = self.wait_run(client, result.json()["id"], RunStatus.FAILED)
                self.assertIn("保存失败", failed.error or "")
                self.assertEqual(client.get("/runs").json()["items"][0]["status"], "failed")
            self.assertEqual(tool.calls, [])

    def test_external_document_ingestion_uses_explicit_media_and_version(self) -> None:
        source = "external://fixture/workspaces/1/notes/42"
        document = {"source_uri": source, "title": "工作台资料", "text": "# 数据库\n项目使用 PostgreSQL。",
                    "media_type": "text/markdown", "source_version": "1"}
        with self.client([AIMessage(content="未调用")]) as client:
            created = client.post("/knowledge/documents", json=document)
            self.assertEqual(created.status_code, 200, created.text)
            self.assertEqual(created.json()["status"], "indexed")
            self.assertEqual(client.post("/knowledge/documents", json=document).json()["status"], "unchanged")
            changed = client.post("/knowledge/documents", json={**document, "text": "# 新资料\n保存到 PostgreSQL。", "source_version": "2"})
            self.assertEqual(changed.status_code, 200)
            self.assertEqual(changed.json()["document_id"], created.json()["document_id"])
            saved = build_rag_application(self.settings).repository.get(source)
            assert saved is not None
            self.assertEqual(saved.source.media_type, "text/markdown")
            self.assertEqual(saved.source.metadata["source_version"], "2")
            self.assertEqual(saved.source.title, document["title"])
            for invalid in ({**document, "source_uri": "../outside.md"}, {**document, "media_type": "application/pdf"},
                            {**document, "text": self.settings.agent_service_token}):
                self.assertEqual(client.post("/knowledge/documents", json=invalid).status_code, 422)
            self.assertEqual(client.get("/runs").json()["items"], [])
            self.assertNotIn("ingest_external_document", {tool.name for tool in build_tool_application(self.settings).list_tools()})

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
            self.assertEqual(client.get("/runs").json()["items"], [])

    def test_restarted_service_resumes_approval_with_original_id_once(self) -> None:
        with self.client([call_message("count", {"text": "审批内容"})]) as client:
            response = client.post("/runs", json={"task": "执行操作", "session_id": "approval"})
            run = _RUN.validate_json(response.content)
            self.wait_run(client, run.id, RunStatus.WAITING_APPROVAL)
            previous_steps = [step.id for step in self.wait_run(client, run.id, RunStatus.WAITING_APPROVAL).steps]
            self.assertEqual(client.delete(f"/runs/{run.id}").status_code, 409)
            self.assertEqual(self.tool.calls, [])
            self.assertEqual(client.post("/runs", json={"task": "覆盖任务", "session_id": "approval"}).status_code, 409)
        with self.client([AIMessage(content="完成")]) as client:
            self.assertEqual(client.get(f"/runs/{run.id}").status_code, 200)
            pending = client.get("/sessions/approval/approvals").json()
            self.assertEqual(len(pending), 1)
            bad = client.post("/sessions/approval/resume", json={"decisions": {"old-id": "approve"}})
            self.assertEqual(bad.status_code, 409)
            decisions = {key: "approve" for key in pending}
            resumed = client.post("/sessions/approval/resume", json={"decisions": decisions})
            self.assertEqual(resumed.status_code, 202, resumed.text)
            self.assertEqual(_RUN.validate_json(resumed.content).id, run.id)
            completed = self.wait_run(client, run.id, RunStatus.COMPLETED)
            self.assertTrue(set(previous_steps).issubset({step.id for step in completed.steps}))
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

    def test_session_crud_and_pagination_validate_revision(self) -> None:
        with self.client([AIMessage(content="完成")]) as client:
            created = client.post("/sessions", json={"title": "第一场对话"})
            self.assertEqual(created.status_code, 201, created.text)
            session = created.json()
            path = f"/sessions/{session['id']}"
            self.assertEqual(session["revision"], 0)
            self.assertEqual(client.get(path).json(), session)
            client.post("/sessions", json={"title": "第二场对话"})
            page = client.get("/sessions?limit=1").json()
            self.assertEqual(len(page["items"]), 1)
            self.assertEqual(page["next_offset"], 1)
            self.assertEqual(len(client.get("/sessions?limit=1&offset=1").json()["items"]), 1)
            renamed = client.patch(path, json={"title": "新标题", "revision": 0})
            self.assertEqual(renamed.status_code, 200, renamed.text)
            self.assertEqual(renamed.json()["revision"], 1)
            self.assertEqual(client.patch(path, json={"title": "过期", "revision": 0}).status_code, 409)
            self.assertEqual(client.delete(path + "?revision=0").status_code, 409)
            self.assertEqual(client.delete(path + "?revision=1").status_code, 200)
            self.assertEqual(client.get(path).status_code, 404)
            self.assertEqual(client.get(path + "/messages").status_code, 404)
            for query in ("limit=0", "limit=101", "offset=-1", "limit=no", "unknown=1"):
                self.assertEqual(client.get("/sessions?" + query).status_code, 422)
            self.assertEqual(client.get("/sessions/bad.id").status_code, 422)
            self.assertEqual(client.post("/sessions", json={"title": " "}).status_code, 422)
            self.assertEqual(client.post("/sessions", json={"scope": "other"}).status_code, 422)

    def test_message_crud_persists_without_invoking_agent(self) -> None:
        with self.client([AIMessage(content="完成")]) as client:
            session = client.post("/sessions", json={}).json()
            path = f"/sessions/{session['id']}/messages"
            first = client.post(path, json={"content": "第一条", "revision": 0})
            self.assertEqual(first.status_code, 201, first.text)
            message = first.json()
            self.assertEqual(client.get(path + "/" + message["id"]).json(), message)
            self.assertEqual(client.post(path, json={"content": "旧版本", "revision": 0}).status_code, 409)
            self.assertEqual(client.post(path, json={"content": "第二条", "revision": 1}).status_code, 201)
            self.assertEqual(client.get(path + "?limit=1").json()["next_offset"], 1)
            edited = client.patch(path + "/" + message["id"], json={"content": "改写", "revision": 2})
            self.assertEqual(edited.status_code, 200, edited.text)
            self.assertEqual(len(client.get(path).json()["items"]), 1)
            self.assertEqual(client.get("/runs").json()["items"], [])
            self.assertEqual(client.post(path, json={"content": "伪造", "revision": 3, "role": "assistant"}).status_code, 422)
        with self.client([AIMessage(content="完成")]) as client:
            self.assertEqual(client.get(path).json()["items"][0]["content"], "改写")
            self.assertEqual(client.delete(path + "/" + message["id"] + "?revision=3").status_code, 200)
            self.assertEqual(client.get(path).json()["items"], [])
            self.assertEqual(client.get(path + "/" + message["id"]).status_code, 404)

    def test_run_journal_and_approval_guard_survive_restart(self) -> None:
        with self.client([call_message("count", {}), AIMessage(content="答复")]) as client:
            run = _RUN.validate_json(client.post("/runs", json={"task": "需审批", "session_id": "guard"}).content)
            self.wait_run(client, run.id, RunStatus.WAITING_APPROVAL)
            session = client.get("/sessions/guard").json()
            path = "/sessions/guard/messages/" + run.id + "_user"
            self.assertEqual(client.patch(path, json={"content": "覆盖", "revision": session["revision"]}).status_code, 409)
            self.assertEqual(client.delete(f"/sessions/guard?revision={session['revision']}").status_code, 409)
        with self.client([AIMessage(content="完成")]) as client:
            session = client.get("/sessions/guard").json()
            self.assertEqual(client.delete(f"/sessions/guard?revision={session['revision']}").status_code, 409)
            pending = client.get("/sessions/guard/approvals").json()
            client.post("/sessions/guard/resume", json={"decisions": {key: "reject" for key in pending}})
            self.wait_run(client, run.id, RunStatus.COMPLETED)
            messages = client.get("/sessions/guard/messages").json()["items"]
            self.assertEqual([message["role"] for message in messages], ["user", "assistant"])
            self.assertEqual({message["run_id"] for message in messages}, {run.id})
            session = client.get("/sessions/guard").json()
            self.assertEqual(client.patch("/sessions/guard/messages/" + messages[1]["id"],
                json={"content": "伪造答复", "revision": session["revision"]}).status_code, 409)

    def test_active_run_prevents_history_deletion(self) -> None:
        tool = BlockingReadTool()
        self.tool = tool
        with self.client([call_message("count", {})]) as client:
            run = _RUN.validate_json(client.post("/runs", json={"task": "等待"}).content)
            self.assertTrue(tool.started.wait(5))
            session = client.get(f"/sessions/{run.session_id}").json()
            response = client.delete(f"/sessions/{run.session_id}?revision={session['revision']}")
            self.assertEqual(response.status_code, 409, response.text)
            client.post(f"/runs/{run.id}/cancel")
            self.wait_run(client, run.id, RunStatus.CANCELLED)


if __name__ == "__main__":
    unittest.main()
