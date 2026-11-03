"""通过真实 MCP 协议和 SDK 模型验证笔记原文摄取，不访问个人工作台。"""

import asyncio
import json
import unittest
from hashlib import sha256

from langchain_core.messages import AIMessage
from mcp import Client
from mcp.types import Tool
from mcp.server import MCPServer
from pydantic import JsonValue

from tests.test_agent import ScriptedModel, call_message
from tests.unit.test_agent_service import InMemorySessions
from tests.workstation_support import note_payload
from workstation_agent.agent.factory import build_agent
from workstation_agent.application.documents import DocumentApplication
from workstation_agent.application.rag import RagApplication
from workstation_agent.application.service import AgentApplication
from workstation_agent.application.tools import ToolApplication
from workstation_agent.bootstrap import RagApprovalPolicy
from workstation_agent.domain.runs import RunStatus
from workstation_agent.integrations.workstation.config import WorkstationNotesConfig
from workstation_agent.integrations.workstation.notes import WorkstationNoteSource, workstation_note_tools
from workstation_agent.rag.config import RagSettings
from workstation_agent.rag.repository import InMemoryIndexRepository
from workstation_agent.safety import PermissionLevel
from workstation_agent.tools.builtin.rag import IngestExternalDocumentTool
from workstation_agent.tools.execution import ToolGrant, approved_call
from workstation_agent.tools.executor import ToolExecutor
from workstation_agent.tools.langchain import make_langchain_tool
from workstation_agent.tools.mcp_remote import RemoteMCPTool, remote_invocation
from workstation_agent.tools.registry import ToolRegistry


class WorkstationNoteTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.calls: list[tuple[int, int]] = []
        self.payload = note_payload()
        self.block: asyncio.Event | None = None
        self.started = asyncio.Event()
        self.server = MCPServer("workstation-note-fixture")
        self.repository = InMemoryIndexRepository()
        self.documents = DocumentApplication(ToolApplication(ToolExecutor(ToolRegistry((
            IngestExternalDocumentTool(RagApplication(self.repository, RagSettings())),
        )), RagApprovalPolicy(approve_write=True))))
        self.binding = WorkstationNotesConfig(server="workstation", workspace_id=11)

        @self.server.tool()
        async def get_note(workspace_id: int, note_id: int) -> dict[str, JsonValue]:
            """按新版 MCP 读取形态返回 SDK 序列化结果。"""
            self.calls.append((workspace_id, note_id))
            self.started.set()
            if self.block is not None:
                await self.block.wait()
            return self.payload

    async def remote(self, client: Client) -> RemoteMCPTool:
        """读取真实工具目录并使用本地配置的只读权限。"""
        tool = next(item for item in (await client.list_tools()).tools if item.name == "get_note")
        return RemoteMCPTool(client, tool, "workstation__get_note", PermissionLevel.SAFE_READ)

    async def test_approved_ingestion_preserves_long_original_and_refreshes_stable_source(self) -> None:
        content = "# 原始标题\r\n" + "超过模型工具输出预算的原文。\r\n" * 5000
        self.payload = note_payload(content=content)
        async with Client(self.server) as client:
            tools = workstation_note_tools(self.binding, await self.remote(client), self.documents, max_file_bytes=1_000_000)
            inspect = remote_invocation(tools[0], 50_000)
            ingest = remote_invocation(tools[1], 50_000)
            summary = json.loads((await inspect({"note_id": 42})).content)
            self.assertEqual(summary["version"], 7)
            self.assertNotIn(content, json.dumps(summary))
            arguments: dict[str, JsonValue] = {"note_id": 42, "expected_version": 7}
            self.assertEqual((await ingest(arguments)).error_code, "permission_required")
            self.assertEqual(self.calls, [(11, 42)])
            with approved_call(ToolGrant.for_call("ingest_workstation_note", arguments, context=tools[1].definition.approval_context)):
                result = await ingest(arguments)
            self.assertTrue(result.success, result.error)
            first = json.loads(result.content)
            source_uri = "workstation://workstation/workspaces/11/notes/42"
            saved = self.repository.get(source_uri)
            assert saved is not None
            self.assertEqual(saved.source.checksum, sha256(content.encode()).hexdigest())
            self.assertEqual(saved.source.metadata["source_version"], "7")
            for chunk in saved.chunks:
                self.assertEqual(chunk.page_content, content[chunk.metadata["source_start"]:chunk.metadata["source_end"]])
            with approved_call(ToolGrant.for_call("ingest_workstation_note", arguments, context=tools[1].definition.approval_context)):
                self.assertEqual(json.loads((await ingest(arguments)).content)["status"], "unchanged")
            self.payload = note_payload(content="# 刷新后的原文\n新资料。", version=8, title="新标题")
            arguments["expected_version"] = 8
            with approved_call(ToolGrant.for_call("ingest_workstation_note", arguments, context=tools[1].definition.approval_context)):
                refreshed = json.loads((await ingest(arguments)).content)
            self.assertEqual(refreshed["document_id"], first["document_id"])
            self.assertEqual(len(self.repository.state().snapshots), 1)

    async def test_graph_rejects_changed_source_after_approval_without_writing(self) -> None:
        async with Client(self.server) as client:
            raw_tools = workstation_note_tools(self.binding, await self.remote(client), self.documents, max_file_bytes=1_000_000)
            tools = [make_langchain_tool(tool.definition, remote_invocation(tool, 50_000)) for tool in raw_tools]
            model = ScriptedModel(responses=[
                call_message("inspect_workstation_note", {"note_id": 42}),
                call_message("ingest_workstation_note", {"note_id": 42, "expected_version": 7}),
                AIMessage(content="来源已变化，请重新确认"),
            ])
            app = AgentApplication(build_agent(model, tools), checkpointer=InMemorySessions())
            run = await app.run_task("导入工作台笔记", session_id="note")
            self.assertEqual(run.status, RunStatus.WAITING_APPROVAL)
            self.assertEqual(self.calls, [(11, 42)])
            pending = await app.pending_approvals("note")
            self.assertEqual(next(iter(pending.values())).arguments, {"note_id": 42, "expected_version": 7})
            self.payload = note_payload(version=8)
            resumed = await app.resume_task("note", {key: "approve" for key in pending})
            self.assertEqual(resumed.status, RunStatus.COMPLETED)
            self.assertEqual(self.repository.state().snapshots, ())

    async def test_rejected_tool_never_reads_source(self) -> None:
        async with Client(self.server) as client:
            raw = workstation_note_tools(self.binding, await self.remote(client), self.documents, max_file_bytes=1_000_000)[1]
            tool = make_langchain_tool(raw.definition, remote_invocation(raw, 50_000))
            app = AgentApplication(build_agent(ScriptedModel(responses=[
                call_message("ingest_workstation_note", {"note_id": 42, "expected_version": 7}), AIMessage(content="已拒绝"),
            ]), [tool]), checkpointer=InMemorySessions())
            await app.run_task("导入笔记", session_id="reject")
            pending = await app.pending_approvals("reject")
            await app.resume_task("reject", {key: "reject" for key in pending})
            self.assertEqual(self.calls, [])
            self.assertEqual(self.repository.state().snapshots, ())

    async def test_bad_response_or_parameters_leave_index_empty(self) -> None:
        async with Client(self.server) as client:
            tool = workstation_note_tools(self.binding, await self.remote(client), self.documents, max_file_bytes=1_000_000)[1]
            invoke = remote_invocation(tool, 50_000)
            args: dict[str, JsonValue] = {"note_id": 42, "expected_version": 7}
            invalid = await invoke({**args, "workspace_id": 99, "text": "模型提供的正文"})
            self.assertEqual(invalid.error_code, "invalid_arguments")
            self.assertEqual(self.calls, [])
            for change in ({"id": 99}, {"version": True}, {"content": 1}, {"version": 8}):
                self.payload = {**note_payload(), **change}
                with approved_call(ToolGrant.for_call("ingest_workstation_note", args, context=tool.definition.approval_context)):
                    result = await invoke(args)
                self.assertFalse(result.success)
            self.assertEqual(self.repository.state().snapshots, ())

    async def test_cancellation_while_reading_never_publishes(self) -> None:
        self.block = asyncio.Event()
        async with Client(self.server) as client:
            tool = workstation_note_tools(self.binding, await self.remote(client), self.documents, max_file_bytes=1_000_000)[1]
            args: dict[str, JsonValue] = {"note_id": 42, "expected_version": 7}
            with approved_call(ToolGrant.for_call("ingest_workstation_note", args, context=tool.definition.approval_context)):
                task = asyncio.ensure_future(remote_invocation(tool, 50_000)(args))
                await asyncio.wait_for(self.started.wait(), 3)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            self.assertEqual(self.repository.state().snapshots, ())

    async def test_old_contract_and_sensitive_or_oversized_sources_are_rejected(self) -> None:
        async with Client(self.server) as client:
            old = RemoteMCPTool(client, Tool(name="get_note", input_schema={"type": "object", "properties": {
                "note_id": {"type": "integer"}}, "required": ["note_id"]}),
                "workstation__get_note", PermissionLevel.SAFE_READ)
            with self.assertRaises(ValueError):
                WorkstationNoteSource(self.binding, old, max_file_bytes=1000)
            tool = workstation_note_tools(self.binding, await self.remote(client), self.documents,
                                          max_file_bytes=1000, secrets=("private-note-secret",))[1]
            args: dict[str, JsonValue] = {"note_id": 42, "expected_version": 7}
            for text, code in (("private-note-secret", "sensitive_content"), ("x" * 1001, "resource_limit")):
                self.payload = note_payload(content=text)
                with approved_call(ToolGrant.for_call("ingest_workstation_note", args, context=tool.definition.approval_context)):
                    result = await remote_invocation(tool, 50_000)(args)
                self.assertEqual(result.error_code, code)
                self.assertNotIn("private-note-secret", result.content)
            self.assertEqual(self.repository.state().snapshots, ())
