"""验证真实文档到官方 Agent 的多次检索链，不验证夹具模型推理。"""

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from langchain_core.messages import AIMessage, ToolMessage

from tests.test_agent import ScriptedModel
from workstation_agent.agent.factory import build_agent
from workstation_agent.application import AgentApplication
from workstation_agent.bootstrap import build_tool_application
from workstation_agent.config import Settings
from workstation_agent.domain import RunStatus
from workstation_agent.tools.langchain import local_tools


def search(query, call_id):
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "search_documents",
                "args": {"query": query},
                "id": call_id,
                "type": "tool_call",
            }
        ],
    )


class RagAgentScenarios(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.settings = Settings(root)
        (root / "routing.md").write_text(
            "# 工具路由\n权限检查组件是 GateKeeper。", encoding="utf-8"
        )
        (root / "permissions.md").write_text("# GateKeeper\n写入需要审批。", encoding="utf-8")
        write = build_tool_application(self.settings, approve_rag_write=True)
        for path in ("routing.md", "permissions.md"):
            result = write.execute_tool("ingest_document", {"path": path})
            self.assertTrue(result.success, result)
        self.tools = local_tools(build_tool_application(self.settings))

    async def test_two_hops_return_both_original_sources_and_citations(self):
        graph = build_agent(
            ScriptedModel(
                responses=[
                    search("权限检查组件", "hop1"),
                    search("GateKeeper 写入审批", "hop2"),
                    AIMessage(content="GateKeeper 检查权限，写入需要审批。"),
                ]
            ),
            self.tools,
        )
        state = await graph.ainvoke(
            {"messages": [{"role": "user", "content": "谁检查工具权限，写入有什么要求？"}]}
        )
        outputs = [message for message in state["messages"] if isinstance(message, ToolMessage)]
        self.assertEqual([m.tool_call_id for m in outputs], ["hop1", "hop2"])
        evidence = [
            item
            for message in outputs
            for item in json.loads(message.artifact["content"])["evidence"]
        ]
        self.assertEqual(
            {item["source_uri"] for item in evidence}, {"routing.md", "permissions.md"}
        )
        self.assertTrue(all(item["citation"] and item["checksum"] for item in evidence))

    async def test_duplicate_search_stops_and_state_does_not_leak_between_runs(self):
        graph = build_agent(
            ScriptedModel(
                responses=[
                    search("GateKeeper", "one"),
                    search("GateKeeper", "two"),
                ]
            ),
            self.tools,
        )
        run = await AgentApplication(graph).run_task("重复查询")
        self.assertEqual(run.status, RunStatus.LIMIT_REACHED)
        self.assertEqual(sum(s.kind.value == "tool" for s in run.steps), 1)
        separate = build_agent(
            ScriptedModel(
                responses=[
                    search("GateKeeper", "one"),
                    AIMessage(content="完成"),
                ]
            ),
            self.tools,
        )
        app = AgentApplication(separate)
        self.assertEqual((await app.run_task("任务一")).status, RunStatus.COMPLETED)
        self.assertEqual((await app.run_task("任务二")).status, RunStatus.COMPLETED)

    async def test_search_budget_stops_distinct_queries(self):
        graph = build_agent(
            ScriptedModel(
                responses=[
                    search("GateKeeper", "one"),
                    search("审批", "two"),
                ]
            ),
            self.tools,
            max_search_calls=1,
        )
        run = await AgentApplication(graph).run_task("查找多个事实")
        self.assertEqual(run.status, RunStatus.LIMIT_REACHED)
        self.assertEqual(sum(s.kind.value == "tool" for s in run.steps), 1)
