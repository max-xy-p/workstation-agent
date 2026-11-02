"""通过官方 MCP 服务验证结果适配、调用前授权与官方图中的审批恢复。"""

import json
import unittest

from langchain_core.messages import AIMessage
from mcp import Client
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError as MCPToolError
from mcp.types import CallToolResult, EmbeddedResource, TextContent, TextResourceContents, Tool, ToolAnnotations

from tests.test_agent import ScriptedModel, call_message
from tests.unit.test_agent_service import InMemorySessions
from workstation_agent.agent.factory import build_agent
from workstation_agent.application.service import AgentApplication
from workstation_agent.domain.runs import RunStatus
from workstation_agent.safety import PermissionLevel
from workstation_agent.tools.execution import ToolGrant, approved_call
from workstation_agent.tools.langchain import make_langchain_tool
from workstation_agent.tools.mcp_remote import RemoteMCPTool, remote_invocation


class ExternalMCPTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.calls: list[str] = []
        self.server = MCPServer("external-fixture")

        @self.server.tool(annotations=ToolAnnotations(read_only_hint=True))
        async def read_item(value: str) -> dict[str, str]:
            """返回标准结构化结果，不使用 Agent 的内部结果封装。"""
            self.calls.append(value)
            return {"content": value, "title": "原始资料"}

        @self.server.tool()
        async def fail_item() -> str:
            """模拟不可信服务把秘密放进错误消息。"""
            raise MCPToolError("private-provider-credential")

        @self.server.tool()
        async def text_item() -> CallToolResult:
            """模拟没有结构化结果的标准文本和内嵌文档响应。"""
            return CallToolResult(content=[TextContent(type="text", text="首段正文"), EmbeddedResource(
                type="resource", resource=TextResourceContents(uri="fixture://note/1", text="文档原文"),
            )])

    async def test_text_and_embedded_resources_are_preserved_without_fetching_links(self) -> None:
        async with Client(self.server) as client:
            definition = next(tool for tool in (await client.list_tools()).tools if tool.name == "text_item")
            remote = RemoteMCPTool(client, definition, "work__text_item", PermissionLevel.SAFE_READ)
            result = await remote_invocation(remote, 1000)({})
            self.assertTrue(result.success)
            self.assertEqual(result.content, "首段正文\n文档原文")

    async def test_standard_result_schema_and_secret_redaction(self) -> None:
        async with Client(self.server) as client:
            catalog = await client.list_tools()
            definition = next(tool for tool in catalog.tools if tool.name == "read_item")
            remote = RemoteMCPTool(client, definition, "work__read_item", PermissionLevel.SAFE_READ,
                                   secrets=("private-token",))
            invoke = remote_invocation(remote, 1000)
            result = await invoke({"value": "正文 private-token"})
            self.assertTrue(result.success)
            self.assertEqual(json.loads(result.content)["title"], "原始资料")
            self.assertNotIn("private-token", result.content)
            invalid = await invoke({"value": 1})
            self.assertEqual(invalid.error_code, "invalid_arguments")
            self.assertEqual(self.calls, ["正文 private-token"])
            failed = RemoteMCPTool(client, next(tool for tool in catalog.tools if tool.name == "fail_item"),
                                   "work__fail_item", PermissionLevel.SAFE_READ)
            failure = await remote_invocation(failed, 1000)({})
            self.assertEqual(failure.error_code, "remote_tool_failed")
            self.assertNotIn("private-provider-credential", failure.content)

    async def test_remote_read_annotation_does_not_bypass_local_write_permission(self) -> None:
        async with Client(self.server) as client:
            definition = next(tool for tool in (await client.list_tools()).tools if tool.name == "read_item")
            remote = RemoteMCPTool(client, definition, "work__read_item", PermissionLevel.CONTROLLED_WRITE)
            invoke = remote_invocation(remote, 1000)
            denied = await invoke({"value": "写操作"})
            self.assertEqual(denied.error_code, "permission_required")
            self.assertEqual(self.calls, [])
            with approved_call(ToolGrant.for_call("work__read_item", {"value": "别的参数"})):
                self.assertEqual((await invoke({"value": "写操作"})).error_code, "permission_required")
            with approved_call(ToolGrant.for_call("work__read_item", {"value": "写操作"})):
                self.assertTrue((await invoke({"value": "写操作"})).success)
            self.assertEqual(self.calls, ["写操作"])

    async def test_graph_approval_happens_before_external_call_and_resumes_once(self) -> None:
        async with Client(self.server) as client:
            definition = next(tool for tool in (await client.list_tools()).tools if tool.name == "read_item")
            remote = RemoteMCPTool(client, definition, "work__read_item", PermissionLevel.DESTRUCTIVE)
            tools = [make_langchain_tool(remote.definition, remote_invocation(remote, 1000))]
            app = AgentApplication(build_agent(ScriptedModel(responses=[
                call_message("work__read_item", {"value": "确认后的操作"}), AIMessage(content="完成"),
            ]), tools), checkpointer=InMemorySessions())
            run = await app.run_task("执行外部工具", session_id="approval")
            self.assertEqual(run.status, RunStatus.WAITING_APPROVAL)
            self.assertEqual(self.calls, [])
            pending = await app.pending_approvals("approval")
            resumed = await app.resume_task("approval", {key: "approve" for key in pending})
            self.assertEqual(resumed.id, run.id)
            self.assertEqual(resumed.status, RunStatus.COMPLETED)
            self.assertEqual(self.calls, ["确认后的操作"])

    async def test_schema_cannot_fetch_external_references(self) -> None:
        async with Client(self.server) as client:
            with self.assertRaises(ValueError):
                RemoteMCPTool(client, Tool(name="unsafe", input_schema={"$ref": "https://example.com/schema.json"}),
                              "unsafe", PermissionLevel.SAFE_READ)
            with self.assertRaises(ValueError):
                RemoteMCPTool(client, Tool(name="unsafe", input_schema={"type": "object", "description": "private-schema-token"}),
                              "unsafe", PermissionLevel.SAFE_READ, secrets=("private-schema-token",))
