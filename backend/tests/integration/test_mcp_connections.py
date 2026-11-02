"""通过实际子进程和 HTTP 服务验证通用连接，不连接个人工作台。"""

import json
from pathlib import Path
import socket
import subprocess
import sys
import time
import unittest

from workstation_agent.config import Settings
from workstation_agent.tools.mcp_client import external_tools
from workstation_agent.tools.mcp_config import MCPServerConfig


class MCPConnectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_stdio_connections_keep_namespaces_and_tool_allowlists(self) -> None:
        workspace = Path(__file__).resolve().parents[2]
        servers = tuple(MCPServerConfig(
            name=name, transport="stdio", command="uv",
            args=["run", "--no-sync", "python", "-m", "tests.serve_external_mcp_fixture"], tools={"read_item": "read"},
        ) for name in ("one", "two"))
        async with external_tools(Settings(workspace, external_mcp_servers=servers)) as tools:
            self.assertEqual({tool.name for tool in tools}, {"one__read_item", "two__read_item"})
            result = json.loads(await tools[0].ainvoke({"value": "来自独立进程"}))
            self.assertTrue(result["success"])
            self.assertEqual(json.loads(result["content"])["content"], "来自独立进程")

    async def test_http_connection_and_missing_tool_fail_without_leaking_details(self) -> None:
        workspace = Path(__file__).resolve().parents[2]
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        process = subprocess.Popen(
            [sys.executable, "-m", "tests.serve_external_mcp_fixture", "--port", str(port)], cwd=workspace,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    self.fail("测试 MCP 服务提前退出")
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                        break
                except OSError:
                    time.sleep(0.05)
            else:
                self.fail("测试 MCP 服务未就绪")
            server = MCPServerConfig(name="http", url=f"http://127.0.0.1:{port}/mcp", tools={"read_item": "read"})
            async with external_tools(Settings(workspace, external_mcp_servers=(server,))) as tools:
                result = json.loads(await tools[0].ainvoke({"value": "HTTP 正文"}))
                self.assertTrue(result["success"])
            invalid = server.model_copy(update={"tools": {"missing_private_name": "read"}})
            with self.assertRaises(ValueError) as caught:
                async with external_tools(Settings(workspace, external_mcp_servers=(invalid,))):
                    self.fail("缺失的白名单工具不能初始化成功")
            self.assertNotIn("missing_private_name", str(caught.exception))
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
