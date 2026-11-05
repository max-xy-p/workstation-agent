"""验证真实 PostgreSQL 下的计划审批、并发发布和批次重放。"""

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from langchain_core.messages import AIMessage

from tests.test_agent import ScriptedModel, call_message
from workstation_agent.bootstrap import build_agent_application, build_memory_application, build_rag_application, build_tool_application
from workstation_agent.config import Settings
from workstation_agent.domain.runs import RunStatus
from workstation_agent.memory.models import MemorySource
from workstation_agent.tools.builtin.memory import MEMORY_SOURCE
from workstation_agent.tools.contracts import ToolError
from workstation_agent.tools.execution import ToolGrant, approved_call


class BusinessWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.settings = Settings(Path(directory.name))

    def test_ingest_approval_binds_original_bytes(self) -> None:
        path = self.settings.workspace / "guide.md"
        path.write_text("审批时的正文", encoding="utf-8")
        tools = build_tool_application(self.settings)
        args = {"path": "guide.md"}
        request = tools.execute_tool("ingest_document", args).approval
        assert request is not None
        self.assertNotIn("审批时的正文", request.model_dump_json())
        path.write_text("审批期间更新的正文", encoding="utf-8")
        with approved_call(ToolGrant(name=request.name, fingerprint=request.fingerprint)):
            self.assertEqual(tools.execute_tool(request.name, args).error_code, "plan_changed")
        rag = build_rag_application(self.settings)
        self.assertIsNone(rag.repository.get("guide.md"))
        updated = tools.execute_tool(request.name, args).approval
        assert updated is not None
        with approved_call(ToolGrant(name=updated.name, fingerprint=updated.fingerprint)):
            self.assertTrue(tools.execute_tool(updated.name, args).success)
        self.assertEqual(json.loads(tools.execute_tool(updated.name, args).content)["status"], "unchanged")

    def test_index_deleted_during_build_is_not_recreated(self) -> None:
        rag = build_rag_application(self.settings)
        rag.ingest("guide.md", "初版正文", {})
        plan = rag.plan_ingestion("guide.md", "准备替换的正文", {})

        def delete_during_build() -> dict[str, tuple[float, ...]]:
            self.assertTrue(rag.repository.delete("guide.md"))
            return {}

        with patch.object(rag, "_embed", side_effect=lambda chunks: delete_during_build()):
            with self.assertRaises(ToolError) as caught:
                rag.commit_ingestion(plan)
        self.assertEqual(caught.exception.code, "version_conflict")
        self.assertIsNone(rag.repository.get("guide.md"))

    def test_memory_batch_checks_target_version_and_does_not_replay(self) -> None:
        tools = build_tool_application(self.settings)
        memory = build_memory_application(self.settings, approve_write=True)
        initial = memory.execute("remember_memory", {"content": "初始约定"})
        args = {"entries": [{"content": "修改约定", "target_id": initial["id"], "expected_version": 1},
                            {"content": "另一条约定"}]}
        token = MEMORY_SOURCE.set(MemorySource(origin="explicit", run_id="test_batch"))
        self.addCleanup(MEMORY_SOURCE.reset, token)
        request = tools.execute_tool("save_memory_batch", args).approval
        assert request is not None
        memory.execute("update_memory", {"memory_id": initial["id"], "expected_version": 1, "content": "其他修改"})
        with approved_call(ToolGrant(name=request.name, fingerprint=request.fingerprint)):
            self.assertEqual(tools.execute_tool(request.name, args).error_code, "version_conflict")
        self.assertEqual(memory.execute("list_memories", {})["total"], 1)
        args["entries"][0]["expected_version"] = 2
        fresh = tools.execute_tool("save_memory_batch", args).approval
        assert fresh is not None
        with approved_call(ToolGrant(name=fresh.name, fingerprint=fresh.fingerprint)):
            self.assertEqual(json.loads(tools.execute_tool(fresh.name, args).content)["saved"], 2)
        self.assertEqual(json.loads(tools.execute_tool(fresh.name, args).content)["status"], "already_processed")
        self.assertEqual(memory.execute("get_memory", {"memory_id": initial["id"]})["version"], 3)


class WorkflowApprovalResumeTests(unittest.IsolatedAsyncioTestCase):
    async def test_memory_plan_resumes_in_same_run_after_restart(self) -> None:
        with TemporaryDirectory() as directory:
            settings = Settings(Path(directory))
            model = ScriptedModel(responses=[call_message("save_memory_batch", {
                "entries": [{"content": "使用中文注释"}, {"content": "用 uv 管理依赖"}],
            })])
            with patch("workstation_agent.models.providers.openai.build_chat_model", return_value=model):
                async with build_agent_application(settings, transport="local", persist_session=True) as app:
                    run = await app.run_task("把两条项目约定一起记住", session_id="batch_approval")
                    self.assertEqual(run.status, RunStatus.WAITING_APPROVAL)
                    pending = await app.pending_approvals(run.session_id)
                    self.assertEqual(next(iter(pending.values())).arguments["scope"], "workspace")
            memory = build_memory_application(settings)
            self.assertEqual(memory.execute("list_memories", {})["total"], 0)
            resumed_model = ScriptedModel(responses=[AIMessage(content="已保存两条约定")])
            with patch("workstation_agent.models.providers.openai.build_chat_model", return_value=resumed_model):
                async with build_agent_application(settings, transport="local", persist_session=True) as app:
                    resumed = await app.resume_task(run.session_id, {key: "approve" for key in pending})
                    self.assertEqual(resumed.id, run.id)
                    self.assertEqual(resumed.status, RunStatus.COMPLETED, resumed.error)
            self.assertEqual(memory.execute("list_memories", {})["total"], 2)
