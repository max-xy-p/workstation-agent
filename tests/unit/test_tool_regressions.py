from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from workstation_agent.tools.contracts import ToolDefinition
from workstation_agent.safety import AllowSafeReadsPolicy, PermissionLevel, PermissionRequired
from workstation_agent.tools import ToolExecutor, ToolRegistry, default_tools


class ExplodingTool:
    permission = PermissionLevel.SAFE_READ
    definition = ToolDefinition(name="explode", description="test", parameters={})

    def invoke(self, arguments: dict) -> str:
        raise RuntimeError("private-token-for-regression")


class SensitiveTool:
    permission = PermissionLevel.SENSITIVE
    definition = ToolDefinition(name="sensitive", description="test", parameters={})

    def __init__(self) -> None:
        self.called = False

    def invoke(self, arguments: dict) -> str:
        self.called = True
        return "executed"


class ToolRegressionTests(unittest.TestCase):
    def test_numeric_path_is_rejected_instead_of_coerced(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "123").mkdir()
            executor = ToolExecutor(default_tools(root), AllowSafeReadsPolicy())
            self.assertFalse(executor.execute("list_files", {"path": 123}).success)

    def test_extra_arguments_are_rejected(self) -> None:
        with TemporaryDirectory() as directory:
            executor = ToolExecutor(default_tools(Path(directory)), AllowSafeReadsPolicy())
            self.assertFalse(executor.execute("list_files", {"unexpected": "value"}).success)

    def test_unexpected_exception_does_not_expose_its_message(self) -> None:
        executor = ToolExecutor(ToolRegistry((ExplodingTool(),)), AllowSafeReadsPolicy())
        result = executor.execute("explode", {})
        self.assertFalse(result.success)
        self.assertNotIn("private-token-for-regression", result.content)
        self.assertNotIn("private-token-for-regression", result.error or "")

    def test_executor_entry_checks_permission(self) -> None:
        tool = SensitiveTool()
        registry = ToolRegistry((tool,))
        with self.assertRaises(PermissionRequired):
            ToolExecutor(registry, AllowSafeReadsPolicy()).execute("sensitive", {})
        self.assertFalse(tool.called)


if __name__ == "__main__":
    unittest.main()
