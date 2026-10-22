from __future__ import annotations

import unittest

from workstation_agent.application import ToolApplication
from workstation_agent.tools import ToolExecutor, ToolRegistry
from tests.unit.test_tool_executor import EchoTool, RecordingPolicy


class ToolWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tool = EchoTool()
        self.policy = RecordingPolicy()
        self.application = ToolApplication(ToolExecutor(ToolRegistry((self.tool,)), self.policy))

    def test_success_stream_uses_graph_nodes_and_exposes_only_final_result(self) -> None:
        updates = list(self.application.stream_tool("echo", {"text": "hello"}))
        self.assertEqual([update.node for update in updates], ["validate_request", "execute_tool"])
        self.assertIsNone(updates[0].result)
        self.assertEqual(updates[-1].result.content, "hello")

    def test_invalid_envelope_routes_to_end_without_execution(self) -> None:
        updates = list(self.application.stream_tool("echo", []))
        self.assertEqual([update.node for update in updates], ["validate_request"])
        self.assertEqual(updates[0].result.error_code, "invalid_arguments")
        self.assertFalse(self.tool.called)
        self.assertIsNone(self.policy.arguments)

    def test_calls_have_isolated_graph_state(self) -> None:
        failed = self.application.execute_tool("echo", [])
        successful = self.application.execute_tool("echo", {"text": "second request"})
        self.assertFalse(failed.success)
        self.assertTrue(successful.success)
        self.assertEqual(successful.content, "second request")


if __name__ == "__main__":
    unittest.main()
