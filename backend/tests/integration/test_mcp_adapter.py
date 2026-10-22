from __future__ import annotations

import json
import unittest
from pathlib import Path

from mcp import Client
from pydantic import TypeAdapter

from workstation_agent.application.tools import ToolApplication
from workstation_agent.bootstrap import build_tool_application
from workstation_agent.config import Settings
from workstation_agent.interfaces.mcp_server import build_local_service, build_mcp_server
from workstation_agent.safety import AllowSafeReadsPolicy, PermissionLevel
from workstation_agent.tools import (
    PydanticTool,
    ToolArguments,
    ToolExecutor,
    ToolRegistry,
    ToolResult,
)


class BlockedTool(PydanticTool[ToolArguments]):
    name = "blocked"
    description = "Tool that must require approval."
    arguments_model = ToolArguments
    permission = PermissionLevel.CONTROLLED_WRITE

    def run(self, arguments: ToolArguments) -> str:
        raise AssertionError("a blocked tool must not run")


class ExplodingTool(BlockedTool):
    name = "explode"
    permission = PermissionLevel.SAFE_READ

    def run(self, arguments: ToolArguments) -> str:
        raise RuntimeError("private-provider-credential")


class MCPAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_mcp_keeps_schema_and_result_contract(self) -> None:
        application = build_tool_application(Settings(Path.cwd()))
        async with Client(build_mcp_server(application)) as client:
            catalog = await client.list_tools()
            for tool in catalog.tools:
                original = next(t for t in application.tool_catalog() if t["name"] == tool.name)
                self.assertEqual(tool.input_schema, original["parameters"])
                self.assertEqual(tool.output_schema, TypeAdapter(ToolResult).json_schema())
                self.assertEqual(
                    tool.annotations.read_only_hint, original["permission"] == "SAFE_READ"
                )
            result = await client.call_tool("read_text_file", {})
            self.assertTrue(result.is_error)
            self.assertEqual(result.structured_content["error_code"], "invalid_arguments")
            self.assertEqual(json.loads(result.content[0].text), result.structured_content)

    async def test_remote_calls_cannot_bypass_permissions_or_leak_exceptions(self) -> None:
        executor = ToolExecutor(
            ToolRegistry((BlockedTool(), ExplodingTool())), AllowSafeReadsPolicy()
        )
        async with Client(build_mcp_server(ToolApplication(executor))) as client:
            denied = await client.call_tool("blocked", {})
            self.assertTrue(denied.is_error)
            self.assertEqual(denied.structured_content["error_code"], "permission_required")
            failed = await client.call_tool("explode", {})
            self.assertEqual(failed.structured_content["error_code"], "execution_failed")
            self.assertNotIn("private-provider-credential", failed.model_dump_json())

    async def test_service_refuses_missing_token_or_workspace(self) -> None:
        application = build_tool_application(Settings(Path.cwd()))
        missing_token = Settings(Path.cwd())
        missing_workspace = Settings(
            Path.cwd() / "nonexistent-workstation-directory",
            service_token="x" * 32,
        )
        with self.assertRaises(ValueError):
            build_local_service(application, missing_token)
        with self.assertRaises(ValueError):
            build_local_service(application, missing_workspace)
