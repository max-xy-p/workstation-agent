"""验证外部 MCP 可信配置、凭据引用和公开工具名称，不建立网络连接。"""

import json
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from workstation_agent.config import Settings
from workstation_agent.tools.mcp_config import MCPServerConfig, external_servers_from_env
from workstation_agent.tools.mcp_remote import public_tool_name
from workstation_agent.cli import main


class ExternalMCPConfigTests(unittest.TestCase):
    def test_defaults_and_credentials_are_not_serialized(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(external_servers_from_env(), ())
        server = MCPServerConfig(name="workspace", url="http://127.0.0.1:8000/mcp",
                                 token_env="TEST_MCP_TOKEN", tools={"read_item": "read"})
        with patch.dict("os.environ", {"TEST_MCP_TOKEN": "private-credential"}):
            settings = Settings(Path.cwd(), external_mcp_servers=(server,))
            self.assertNotIn("private-credential", repr(settings))
            self.assertIn("private-credential", settings.secrets)
            self.assertEqual(server.credential(), "private-credential")
        with patch.dict("os.environ", {}, clear=True), self.assertRaises(ValueError):
            server.credential()

    def test_invalid_or_ambiguous_configuration_is_rejected(self) -> None:
        valid = {"name": "workspace", "url": "http://127.0.0.1:8000/mcp", "tools": {"read_item": "read"}}
        for values in ([valid, valid], [{**valid, "tools": {}}], [{**valid, "tools": {"x": "auto"}}],
                       [{**valid, "url": "http://private-token@example.com/mcp"}],
                       [{**valid, "url": "http://example.com/mcp"}], [{**valid, "url": "https://example.com/mcp?token=private-token"}],
                       [{**valid, "command": "python"}], [{**valid, "unknown": "private-token"}]):
            with self.subTest(values=values), patch.dict("os.environ", {"WORKSTATION_AGENT_MCP_SERVERS": json.dumps(values)}):
                with self.assertRaises(ValueError) as caught:
                    external_servers_from_env()
                self.assertNotIn("private-token", str(caught.exception))

    def test_stdio_distinguishes_plain_environment_from_secret_values(self) -> None:
        server = MCPServerConfig(name="work", transport="stdio", command="uv", env=["PROJECT_NUMBER"],
                                 secret_env=["CLIENT_SECRET"], tools={"read_item": "read"})
        with patch.dict("os.environ", {"PROJECT_NUMBER": "1", "CLIENT_SECRET": "private-credential"}):
            settings = Settings(Path.cwd(), external_mcp_servers=(server,))
            self.assertIn("private-credential", settings.secrets)
            self.assertNotIn("1", settings.secrets)

    def test_namespace_handles_collisions_and_long_names(self) -> None:
        self.assertNotEqual(public_tool_name("one", "read"), public_tool_name("two", "read"))
        self.assertNotEqual(public_tool_name("one", "a.b"), public_tool_name("one", "a_b"))
        self.assertLessEqual(len(public_tool_name("one", "a" * 128)), 64)
        self.assertEqual(public_tool_name("one", "read"), "one__read")

    def test_doctor_checks_stdio_path_without_connecting(self) -> None:
        configuration = [{"name": "workspace", "transport": "stdio", "command": "uv",
                          "cwd": "../outside", "tools": {"read_item": "read"}}]
        with patch.dict("os.environ", {"WORKSTATION_AGENT_MCP_SERVERS": json.dumps(configuration)}, clear=True), \
                patch("workstation_agent.tools.mcp_client._external_client", side_effect=AssertionError("doctor 不应建立连接")), \
                redirect_stdout(StringIO()):
            self.assertEqual(main(["doctor"]), 1)
