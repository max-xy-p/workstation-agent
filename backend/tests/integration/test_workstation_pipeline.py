"""验证真实应用组装、MCP 原文读取、审批及 PostgreSQL 索引发布的完整连接。"""

import unittest
from contextlib import AsyncExitStack
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from langchain_core.messages import AIMessage, ToolMessage
from mcp import Client
from mcp.server import MCPServer
from pydantic import JsonValue

from tests.test_agent import ScriptedModel, call_message
from tests.workstation_support import note_payload
from workstation_agent.bootstrap import build_agent_application, build_rag_application
from workstation_agent.config import Settings
from workstation_agent.domain.runs import RunStatus
from workstation_agent.integrations.workstation.config import WorkstationNotesConfig
from workstation_agent.memory.config import MemorySettings
from workstation_agent.tools.mcp_config import MCPServerConfig


class WorkstationPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_configured_agent_imports_original_into_postgresql_and_can_search(self) -> None:
        server = MCPServer("workstation-pipeline-fixture")
        calls: list[tuple[int, int]] = []
        original = "# 连接验证\r\n工作台原始资料的唯一内容：冰川数据库约定。"

        @server.tool()
        async def get_note(workspace_id: int, note_id: int) -> dict[str, JsonValue]:
            """只替换外部业务服务，Agent、权限、SDK、RAG 和数据库继续使用真实实现。"""
            calls.append((workspace_id, note_id))
            return note_payload(content=original)

        async def connect(stack: AsyncExitStack, configuration: MCPServerConfig, workspace: Path,
                          timeout: float) -> tuple[Client, tuple[str, ...]]:
            return await stack.enter_async_context(Client(server)), ()

        with TemporaryDirectory() as directory:
            settings = Settings(Path(directory), memory=MemorySettings(enabled=False),
                                external_mcp_servers=(MCPServerConfig(name="notes", url="http://127.0.0.1:8000/mcp",
                                                                     tools={"get_note": "read"}),),
                                workstation_notes=WorkstationNotesConfig(server="notes", workspace_id=11))
            model = ScriptedModel(responses=[call_message("inspect_workstation_note", {"note_id": 42}),
                call_message("ingest_workstation_note", {"note_id": 42, "expected_version": 7}), AIMessage(content="笔记已加入知识库")])
            with patch("workstation_agent.models.providers.openai.build_chat_model", return_value=model), \
                    patch("workstation_agent.tools.mcp_client._external_client", connect):
                async with build_agent_application(settings, transport="local") as app:
                    run = await app.run_task("将笔记加入知识库")
                    self.assertEqual(run.status, RunStatus.WAITING_APPROVAL)
                    self.assertEqual(calls, [(11, 42)])
                    pending = await app.pending_approvals(run.session_id)
                    completed = await app.resume_task(run.session_id, {key: "approve" for key in pending})
                    self.assertEqual(completed.status, RunStatus.COMPLETED, completed.error)
                    self.assertEqual(calls, [(11, 42), (11, 42)])
                    snapshot = await app.graph.aget_state(app.checkpointer.config(run.session_id) if app.checkpointer else {
                        "configurable": {"thread_id": run.session_id},
                    })
                    for message in snapshot.values.get("messages", []):
                        if isinstance(message, ToolMessage):
                            self.assertNotIn(original, str(message.content))
            rag = build_rag_application(settings)
            saved = rag.repository.get("workstation://notes/workspaces/11/notes/42")
            assert saved is not None
            self.assertEqual(saved.source.metadata["source_version"], "7")
            result = rag.search("冰川数据库约定", {}, 6)
            self.assertEqual(result["status"], "ok")
            self.assertTrue(result["evidence"])

    async def test_restarted_agent_cannot_apply_old_approval_to_another_workspace(self) -> None:
        server = MCPServer("workstation-scope-fixture")
        calls: list[tuple[int, int]] = []

        @server.tool()
        async def get_note(workspace_id: int, note_id: int) -> dict[str, JsonValue]:
            """不同工作区可有相同笔记 ID 与版本，审批仍必须区分来源。"""
            calls.append((workspace_id, note_id))
            return note_payload()

        async def connect(stack: AsyncExitStack, configuration: MCPServerConfig, workspace: Path,
                          timeout: float) -> tuple[Client, tuple[str, ...]]:
            return await stack.enter_async_context(Client(server)), ()

        with TemporaryDirectory() as directory:
            settings = Settings(Path(directory), memory=MemorySettings(enabled=False),
                                external_mcp_servers=(MCPServerConfig(name="notes", url="http://127.0.0.1:8000/mcp",
                                                                     tools={"get_note": "read"}),),
                                workstation_notes=WorkstationNotesConfig(server="notes", workspace_id=11))
            with patch("workstation_agent.models.providers.openai.build_chat_model", return_value=ScriptedModel(responses=[
                    call_message("ingest_workstation_note", {"note_id": 42, "expected_version": 7}),
            ])), patch("workstation_agent.tools.mcp_client._external_client", connect):
                async with build_agent_application(settings, transport="local", persist_session=True) as app:
                    run = await app.run_task("导入笔记", session_id="scope")
                    self.assertEqual(run.status, RunStatus.WAITING_APPROVAL)
                    old_pending = await app.pending_approvals("scope")
            changed = replace(settings, workstation_notes=WorkstationNotesConfig(server="notes", workspace_id=22))
            with patch("workstation_agent.models.providers.openai.build_chat_model", return_value=ScriptedModel(responses=[
                    AIMessage(content="停止"),
            ])), patch("workstation_agent.tools.mcp_client._external_client", connect):
                async with build_agent_application(changed, transport="local", persist_session=True) as app:
                    resumed = await app.resume_task("scope", {key: "approve" for key in old_pending})
                    self.assertEqual(resumed.status, RunStatus.FAILED)
            self.assertEqual(calls, [])
            self.assertEqual(build_rag_application(settings).repository.state().snapshots, ())
