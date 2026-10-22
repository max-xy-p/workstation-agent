from __future__ import annotations

import unittest

from langchain_core.messages import AIMessage

from workstation_agent.agent.factory import build_agent
from workstation_agent.application import AgentApplication, ToolApplication
from workstation_agent.domain import RunStatus
from workstation_agent.safety import AllowSafeReadsPolicy, PermissionLevel
from workstation_agent.tools import ToolExecutor, ToolRegistry
from workstation_agent.tools.langchain import local_tools
from tests.test_agent import ScriptedModel, call_message
from tests.unit.test_tool_executor import EchoTool


class SensitiveTool(EchoTool):
    permission = PermissionLevel.SENSITIVE


class FailingModel(ScriptedModel):
    """通过原始异常验证应用层不会向调用方泄漏供应商凭据。"""
    async def _agenerate(self, *args, **kwargs):
        raise RuntimeError("private-provider-token")


class OrchestratorTests(unittest.IsolatedAsyncioTestCase):
    async def test_exposes_official_graph_progress(self):
        graph = build_agent(ScriptedModel(responses=[AIMessage(content="finished")]), [])
        nodes = []
        run = await AgentApplication(graph).run_task("工作", on_node=nodes.append)
        self.assertEqual(run.status, RunStatus.COMPLETED)
        self.assertIn("model", nodes)
        self.assertIn("ModelCallLimitMiddleware.before_model", nodes)

    async def test_sensitive_tool_waits_for_approval_without_execution(self):
        tool = SensitiveTool()
        application = ToolApplication(ToolExecutor(ToolRegistry((tool,)), AllowSafeReadsPolicy()))
        graph = build_agent(ScriptedModel(responses=[call_message("echo", {})]), local_tools(application))
        run = await AgentApplication(graph).run_task("敏感操作")
        self.assertEqual(run.status, RunStatus.WAITING_APPROVAL)
        self.assertFalse(tool.called)

    async def test_provider_exception_is_sanitized(self):
        graph = build_agent(FailingModel(responses=[AIMessage(content="unused")]), [])
        run = await AgentApplication(graph).run_task("工作")
        self.assertEqual(run.status, RunStatus.FAILED)
        self.assertNotIn("private-provider-token", run.error)
