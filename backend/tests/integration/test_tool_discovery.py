"""验证分层发现实际改变模型绑定，并沿用远程工具审批和会话恢复。"""

import unittest
from unittest.mock import patch

from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from mcp import Client
from mcp.server import MCPServer
from pydantic import JsonValue

from tests.test_agent import ScriptedModel
from tests.unit.test_agent_service import InMemorySessions
from workstation_agent.agent.factory import build_agent
from workstation_agent.application.service import AgentApplication
from workstation_agent.domain.runs import RunStatus
from workstation_agent.integrations.workstation.discovery import WORKSTATION_GROUPS, workstation_discovery_entries
from workstation_agent.safety import PermissionLevel
from workstation_agent.tools.discovery import FindToolArguments, ToolDiscovery
from workstation_agent.tools.langchain import make_langchain_tool
from workstation_agent.tools.mcp_config import MCPServerConfig
from workstation_agent.tools.mcp_remote import RemoteMCPTool, remote_invocation


def call(name: str, arguments: dict[str, JsonValue], identifier: str) -> AIMessage:
    """模拟一条具有独立调用 ID 的模型工具请求。"""
    return AIMessage(content="", tool_calls=[{"name": name, "args": arguments, "id": identifier}])


class ToolDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_scoped_binding_approval_resume_and_new_run(self) -> None:
        server = MCPServer("discovery-fixture")
        writes: list[int] = []

        @server.tool()
        async def link_task_issue(task_id: int) -> str:
            """将已有任务与 GitHub issue 关联。"""
            writes.append(task_id)
            return "关联成功"

        @server.tool()
        async def list_notes() -> str:
            """按标题查看笔记。"""
            return "笔记"

        config = MCPServerConfig(name="work", url="http://127.0.0.1:8000/mcp",
                                 tool_discovery="workstation",
                                 tools={"link_task_issue": "write", "list_notes": "read", "get_project": "read"})
        async with Client(server) as client:
            registered = []
            for item in (await client.list_tools()).tools:
                permission = PermissionLevel.SAFE_READ if item.name == "list_notes" else PermissionLevel.CONTROLLED_WRITE
                remote = RemoteMCPTool(client, item, f"work__{item.name}", permission)
                registered.append(make_langchain_tool(remote.definition, remote_invocation(remote, 1000)))
            discovery = ToolDiscovery(registered, workstation_discovery_entries(config), WORKSTATION_GROUPS)
            roots = discovery.find(FindToolArguments())
            self.assertEqual(set(roots["groups"]), {"tasks", "notes"})
            found = discovery.find(FindToolArguments(group="tasks", query="关联已有 issue"))
            self.assertEqual([item["name"] for item in found["tools"]], ["work__link_task_issue"])
            saver = InMemorySaver()
            parallel = call("find_tools", {"group": "tasks", "query": "关联已有 issue"}, "found")
            parallel.tool_calls.extend(call("work__link_task_issue", {"task_id": 55}, "parallel_guess").tool_calls)
            model = ScriptedModel(responses=[
                call("work__link_task_issue", {"task_id": 42}, "guessed"),
                call("find_tools", {}, "groups"),
                parallel,
                call("work__link_task_issue", {"task_id": 42}, "approved"),
            ])
            with patch.object(ScriptedModel, "bind_tools", autospec=True, side_effect=ScriptedModel.bind_tools) as bound:
                graph = build_agent(model, registered, checkpointer=saver, discovery=discovery)
                app = AgentApplication(graph, checkpointer=InMemorySessions())
                run = await app.run_task("关联 GitHub issue", session_id="discovery")
                self.assertEqual(run.status, RunStatus.WAITING_APPROVAL, run.error)
                self.assertEqual(writes, [])
                rounds = [{tool.name for tool in entry.args[1]} for entry in bound.call_args_list]
                self.assertTrue(rounds)
                self.assertIn("find_tools", rounds[0])
                self.assertNotIn("work__link_task_issue", rounds[0])
                self.assertIn("work__link_task_issue", rounds[-1])
                self.assertNotIn("work__list_notes", rounds[-1])
            approvals = await app.pending_approvals("discovery")
            self.assertEqual(len(approvals), 1)
            resumed_model = ScriptedModel(responses=[
                AIMessage(content="已关联"),
                call("work__link_task_issue", {"task_id": 99}, "old_selection"),
                AIMessage(content="需要先查找"),
            ])
            restarted = AgentApplication(
                build_agent(resumed_model, registered, checkpointer=saver, discovery=discovery),
                checkpointer=InMemorySessions(),
            )
            result = await restarted.resume_task("discovery", {key: "approve" for key in approvals})
            self.assertEqual(result.status, RunStatus.COMPLETED, result.error)
            self.assertEqual(writes, [42])
            await restarted.run_task("另一项任务", session_id="discovery")
            self.assertEqual(writes, [42])
