from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory, TemporaryFile
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener, ProxyHandler

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client


class LocalServiceIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.directory = TemporaryDirectory()
        cls.addClassCleanup(cls.directory.cleanup)
        cls.workspace = Path(cls.directory.name)
        (cls.workspace / "hello.txt").write_text("你好，本机服务", encoding="utf-8")
        (cls.workspace / "large.txt").write_text("x" * 10000, encoding="utf-8")
        (cls.workspace / "binary.dat").write_bytes(bytes([255, 254]))
        cls.secret = "integration_test_credential_" + "x" * 32
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            cls.port = probe.getsockname()[1]
        cls.base_url = f"http://127.0.0.1:{cls.port}"
        cls.env = os.environ.copy()
        cls.env.update({
            "WORKSTATION_AGENT_WORKSPACE": str(cls.workspace),
            "WORKSTATION_AGENT_SERVICE_PORT": str(cls.port),
            "WORKSTATION_AGENT_SERVICE_TOKEN": cls.secret,
            "WORKSTATION_AGENT_TOOL_OUTPUT_CHARS": "256",
            "WORKSTATION_AGENT_MAX_STEPS": "8",
            "PYTHONIOENCODING": "utf-8",
        })
        cls.output = TemporaryFile()
        cls.addClassCleanup(cls.output.close)
        cls.process = subprocess.Popen(
            [sys.executable, "-m", "workstation_agent", "serve"], env=cls.env,
            stdout=cls.output, stderr=cls.output,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        cls.addClassCleanup(cls.stop_server)
        cls.opener = build_opener(ProxyHandler({}))
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if cls.process.poll() is not None:
                raise AssertionError("local service exited before becoming ready")
            try:
                with cls.opener.open(cls.base_url + "/health", timeout=0.5) as response:
                    if response.status == 200:
                        return
            except (URLError, TimeoutError):
                time.sleep(0.1)
        raise AssertionError("local service readiness timeout")

    @classmethod
    def stop_server(cls) -> None:
        if cls.process.poll() is None:
            cls.process.terminate()
            try:
                cls.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                cls.process.kill()
                cls.process.wait(timeout=5)
        cls.output.seek(0)
        logs = cls.output.read().decode("utf-8", errors="replace")
        if cls.secret in logs or "你好，本机服务" in logs:
            raise AssertionError("service leaked credentials or tool content to logs")

    async def connect_and_call(self, name: str, arguments: dict, *, mode: str = "auto"):
        async with httpx2.AsyncClient(
            headers={"Authorization": f"Bearer {self.secret}"}, trust_env=False, timeout=10,
        ) as http:
            async with Client(
                streamable_http_client(self.base_url + "/mcp", http_client=http), mode=mode,
            ) as client:
                return await client.call_tool(name, arguments)

    def http_request(self, *, headers: dict | None = None, body: bytes = b"{}") -> tuple[int, str]:
        request = Request(self.base_url + "/mcp", data=body, headers={
            "Content-Type": "application/json", "Accept": "application/json, text/event-stream",
            **(headers or {}),
        })
        try:
            with self.opener.open(request, timeout=5) as response:
                return response.status, response.read().decode()
        except HTTPError as exc:
            return exc.code, exc.read().decode()

    def test_actual_network_discovery_and_utf8_read(self) -> None:
        async def read_only():
            result = await self.connect_and_call("read_text_file", {"path": "hello.txt"})
            self.assertFalse(result.is_error)
            self.assertEqual(result.structured_content["content"], "你好，本机服务")
            async with httpx2.AsyncClient(
                headers={"Authorization": f"Bearer {self.secret}"}, trust_env=False,
            ) as http:
                async with Client(streamable_http_client(self.base_url + "/mcp", http_client=http)) as client:
                    catalog = await client.list_tools()
                    self.assertEqual({t.name for t in catalog.tools}, {"list_files", "read_text_file", "ingest_document", "search_documents", "delete_document", "rag_status"})
                    listing = await client.call_tool("list_files", {})
                    self.assertEqual(
                        {item["name"] for item in json.loads(listing.structured_content["content"])},
                        {"hello.txt", "large.txt", "binary.dat"},
                    )
        asyncio.run(read_only())

    def test_legacy_initialize_client_is_compatible(self) -> None:
        result = asyncio.run(self.connect_and_call("read_text_file", {"path": "hello.txt"}, mode="legacy"))
        self.assertFalse(result.is_error)
        self.assertEqual(result.structured_content["content"], "你好，本机服务")

    def test_normalized_tool_failures_over_network(self) -> None:
        async def scenario():
            for name, arguments, code in [
                ("read_text_file", {}, "invalid_arguments"),
                ("read_text_file", {"path": 123}, "invalid_arguments"),
                ("read_text_file", {"path": "../secret.txt"}, "path_outside_workspace"),
                ("read_text_file", {"path": "missing.txt"}, "not_found"),
                ("read_text_file", {"path": "binary.dat"}, "invalid_utf8"),
                ("unknown", {}, "unknown_tool"),
            ]:
                with self.subTest(code=code):
                    result = await self.connect_and_call(name, arguments)
                    self.assertTrue(result.is_error)
                    self.assertEqual(result.structured_content["error_code"], code)
        asyncio.run(scenario())

    def test_concurrent_clients_keep_results_independent(self) -> None:
        async def scenario():
            good, missing = await asyncio.gather(
                self.connect_and_call("read_text_file", {"path": "hello.txt"}),
                self.connect_and_call("read_text_file", {"path": "missing.txt"}),
            )
            self.assertFalse(good.is_error)
            self.assertEqual(good.structured_content["content"], "你好，本机服务")
            self.assertTrue(missing.is_error)
            self.assertEqual(missing.structured_content["error_code"], "not_found")
        asyncio.run(scenario())

    def test_large_response_is_bounded(self) -> None:
        result = asyncio.run(self.connect_and_call("read_text_file", {"path": "large.txt"}))
        self.assertLessEqual(len(result.structured_content["content"]), 256)
        self.assertTrue(result.structured_content["content"].endswith("[truncated]"))

    def test_authentication_host_and_origin_are_enforced(self) -> None:
        for headers, expected in [
            ({}, 401),
            ({"Authorization": "Bearer wrong-token"}, 401),
            ({"Authorization": f"Bearer {self.secret}", "Origin": "https://untrusted.invalid"}, 403),
            ({"Authorization": f"Bearer {self.secret}", "Host": "untrusted.invalid"}, 421),
        ]:
            with self.subTest(expected=expected):
                status, body = self.http_request(headers=headers)
                self.assertEqual(status, expected)
                self.assertNotIn(self.secret, body)
                self.assertNotIn(str(self.workspace), body)

    def test_malformed_and_oversized_requests_do_not_break_service(self) -> None:
        headers = {"Authorization": f"Bearer {self.secret}"}
        status, _ = self.http_request(headers=headers, body=b"bad-json")
        self.assertEqual(status, 400)
        status, _ = self.http_request(headers=headers, body=b"x" * 65537)
        self.assertEqual(status, 413)
        result = asyncio.run(self.connect_and_call("read_text_file", {"path": "hello.txt"}))
        self.assertFalse(result.is_error)

    def test_standalone_agent_client_process(self) -> None:
        example = Path(__file__).resolve().parents[2] / "examples" / "local_agent_client.py"
        process = subprocess.run(
            [sys.executable, str(example), "read_text_file", "--path", "hello.txt"],
            env=self.env, capture_output=True, text=True, encoding="utf-8", timeout=20,
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        discovery, response = [json.loads(line) for line in process.stdout.splitlines()]
        self.assertIn("read_text_file", discovery["tools"])
        self.assertEqual(response["content"], "你好，本机服务")
        self.assertNotIn(self.secret, process.stdout + process.stderr)

    def test_windows_launcher_and_client_share_inherited_credential(self) -> None:
        if sys.platform != "win32":
            self.skipTest("Windows deployment launcher")
        import shutil
        shell = shutil.which("pwsh")
        if shell is None:
            self.skipTest("PowerShell 7 is not installed")
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        environment = {**self.env, "WORKSTATION_AGENT_SERVICE_PORT": str(port)}
        environment.pop("WORKSTATION_AGENT_SERVICE_TOKEN", None)
        command = r"""
$ErrorActionPreference = 'Stop'
$server = & ./scripts/start-local-service.ps1
try {
    if ($server -isnot [System.Diagnostics.Process]) { throw 'launcher must return a process handle' }
    & ./.venv/Scripts/python.exe examples/local_agent_client.py read_text_file --path hello.txt
    if ($LASTEXITCODE -ne 0) { throw 'client failed to call the launched service' }
} finally {
    if (-not $server.HasExited) { $server.Kill(); $server.WaitForExit() }
}
"""
        process = subprocess.run(
            [shell, "-NoProfile", "-Command", command], env=environment,
            cwd=Path(__file__).resolve().parents[2], capture_output=True,
            text=True, encoding="utf-8", timeout=40,
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertIn("Local MCP service ready", process.stdout)
        self.assertIn("你好，本机服务", process.stdout)

    def test_configuration_diagnostics_and_startup_failures(self) -> None:
        for arguments, env, expected in [
            (["doctor", "--service"], self.env, 0),
            (["doctor", "--service"], {**self.env, "WORKSTATION_AGENT_SERVICE_TOKEN": ""}, 1),
            (["serve"], {**self.env, "WORKSTATION_AGENT_SERVICE_TOKEN": ""}, 2),
            (["serve"], self.env, 1),  # 此端口已由本测试启动的服务占用。
        ]:
            with self.subTest(arguments=arguments, expected=expected):
                process = subprocess.run(
                    [sys.executable, "-m", "workstation_agent", *arguments], env=env,
                    capture_output=True, text=True, encoding="utf-8", timeout=20,
                )
                self.assertEqual(process.returncode, expected, process.stderr)
                self.assertNotIn(self.secret, process.stdout + process.stderr)
                self.assertNotIn("Traceback", process.stderr)
