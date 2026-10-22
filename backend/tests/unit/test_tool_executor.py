from __future__ import annotations

import unittest

from pydantic import TypeAdapter, ValidationError

from workstation_agent.application import ToolApplication
from workstation_agent.safety import PermissionLevel, PermissionRequired
from workstation_agent.tools import (
    PydanticTool,
    ToolArguments,
    ToolExecutor,
    ToolRegistry,
    ToolResult,
)


class EchoArguments(ToolArguments):
    text: str = "default"


class EchoTool(PydanticTool[EchoArguments]):
    name = "echo"
    description = "Test typed tool"
    permission = PermissionLevel.SAFE_READ
    arguments_model = EchoArguments

    def __init__(self) -> None:
        self.called = False

    def run(self, arguments: EchoArguments) -> str:
        self.called = True
        return arguments.text


class RecordingPolicy:
    def __init__(self, allowed: bool = True) -> None:
        self.allowed = allowed
        self.arguments = None

    def allows(self, name: str, permission: PermissionLevel, arguments: dict) -> bool:
        self.arguments = arguments
        return self.allowed


class InvalidOutputTool(EchoTool):
    def run(self, arguments: EchoArguments) -> str:
        return {"secret": "private-output-for-regression"}


class ToolExecutorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tool = EchoTool()
        self.policy = RecordingPolicy()
        self.executor = ToolExecutor(ToolRegistry((self.tool,)), self.policy)

    def test_policy_receives_normalized_defaults(self) -> None:
        result = self.executor.execute("echo", {})
        self.assertTrue(result.success)
        self.assertEqual(result.content, "default")
        self.assertEqual(self.policy.arguments, {"text": "default"})

    def test_invalid_arguments_never_reach_policy_or_handler(self) -> None:
        result = self.executor.execute("echo", {"text": 123})
        self.assertEqual(result.error_code, "invalid_arguments")
        self.assertIsNone(self.policy.arguments)
        self.assertFalse(self.tool.called)

    def test_validation_errors_do_not_echo_extra_field_names_or_values(self) -> None:
        result = self.executor.execute("echo", {"private-key-for-regression": "secret-value"})
        self.assertFalse(result.success)
        self.assertNotIn("private-key-for-regression", result.content)
        self.assertNotIn("secret-value", result.error)

    def test_unknown_tool_is_a_normalized_failure(self) -> None:
        result = self.executor.execute("missing", {})
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "unknown_tool")

    def test_non_object_arguments_are_rejected(self) -> None:
        result = self.executor.execute("echo", [])
        self.assertEqual(result.error_code, "invalid_arguments")
        self.assertFalse(self.tool.called)

    def test_denial_does_not_execute_handler(self) -> None:
        self.policy.allowed = False
        with self.assertRaises(PermissionRequired):
            self.executor.execute("echo", {})
        self.assertFalse(self.tool.called)

    def test_direct_application_exposes_permission_denial(self) -> None:
        self.policy.allowed = False
        result = ToolApplication(self.executor).execute_tool("echo", {})
        self.assertEqual(result.error_code, "permission_required")
        self.assertFalse(self.tool.called)

    def test_invalid_output_is_rejected_without_echoing_data(self) -> None:
        executor = ToolExecutor(ToolRegistry((InvalidOutputTool(),)), self.policy)
        result = executor.execute("echo", {})
        self.assertEqual(result.error_code, "invalid_output")
        self.assertNotIn("private-output-for-regression", result.content)

    def test_output_is_bounded_including_truncation_marker(self) -> None:
        executor = ToolExecutor(ToolRegistry((self.tool,)), self.policy, max_output_chars=32)
        result = executor.execute("echo", {"text": "x" * 100})
        self.assertEqual(len(result.content), 32)
        self.assertTrue(result.content.endswith("[truncated]"))

    def test_result_types_are_validated_and_serializable(self) -> None:
        with self.assertRaises(ValidationError):
            ToolResult(tool_name="echo", content=123)
        result = self.executor.execute("echo", {})
        payload = TypeAdapter(ToolResult).dump_json(result)
        self.assertEqual(TypeAdapter(ToolResult).validate_json(payload).content, "default")

    def test_duplicate_names_are_rejected(self) -> None:
        duplicate_tools = (EchoTool(), EchoTool())
        with self.assertRaises(ValueError):
            ToolRegistry(duplicate_tools)

    def test_non_integer_or_non_positive_output_limit_is_rejected(self) -> None:
        registry = ToolRegistry()
        for limit in (0, -1, True, 1.5):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                ToolExecutor(registry, self.policy, max_output_chars=limit)


if __name__ == "__main__":
    unittest.main()
