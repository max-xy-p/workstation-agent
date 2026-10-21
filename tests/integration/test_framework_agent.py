from __future__ import annotations

import json
import socket
import subprocess
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryFile
from typing import ClassVar

import httpx2
from pydantic import TypeAdapter

from workstation_agent.application.agent_service import ServiceRun

from tests.integration import test_local_service as service_fixture


class CompatibleAPIHandler(BaseHTTPRequestHandler):
    """兼容 HTTP 接口夹具，只验证官方 SDK 请求与响应，不验证模型推理质量。"""
    requests = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.requests.append(body)
        messages = body["messages"]
        task = next(m["content"] for m in messages if m["role"] == "user")
        results = [m for m in messages if m["role"] == "tool"]
        if "HTTP失败" in task:
            self.respond(401, {"error": {"message": "private-provider-token", "type": "authentication_error"}})
            return
        if results:
            result = json.loads(results[-1]["content"])
            text = result["content"] if result["success"] else result["error_code"]
            message = {"role": "assistant", "content": text}
            finish = "stop"
        else:
            arguments = {} if "缺失参数" in task else {"path": "../secret.txt" if "越界" in task else "hello.txt"}
            message = {"role": "assistant", "content": None, "tool_calls": [{
                "id": "call_fixture_1", "type": "function",
                "function": {"name": "read_text_file", "arguments": json.dumps(arguments)},
            }]}
            finish = "tool_calls"
        self.respond(200, {
            "id": "chatcmpl-fixture", "object": "chat.completion", "created": 0,
            "model": "test-model", "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
        })

    def respond(self, status, value):
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class FrameworkAgentIntegrationTests(unittest.TestCase):
    # 下列字段由复用的本机服务夹具在类初始化时填入，声明类型便于检查调用方。
    env: ClassVar[dict[str, str]]
    workspace: ClassVar[Path]
    secret: ClassVar[str]

    @classmethod
    def stop_server(cls):
        service_fixture.LocalServiceIntegrationTests.stop_server.__func__(cls)

    @classmethod
    def setUpClass(cls):
        service_fixture.LocalServiceIntegrationTests.setUpClass.__func__(cls)
        cls.api = ThreadingHTTPServer(("127.0.0.1", 0), CompatibleAPIHandler)
        thread = threading.Thread(target=cls.api.serve_forever, daemon=True)
        thread.start()
        def close_api():
            cls.api.shutdown()
            cls.api.server_close()
            thread.join(timeout=5)
        cls.addClassCleanup(close_api)
        cls.env.update({
            "WORKSTATION_AGENT_MODEL": "test-model",
            "WORKSTATION_AGENT_API_KEY": "test-api-key-private",
            "WORKSTATION_AGENT_BASE_URL": f"http://127.0.0.1:{cls.api.server_port}/v1",
            "WORKSTATION_AGENT_MODEL_TIMEOUT": "10",
            "WORKSTATION_AGENT_CONTEXT_WINDOW": "32768",
            "WORKSTATION_AGENT_MAX_STEPS": "4",
            "WORKSTATION_AGENT_MAX_TOOL_CALLS": "4",
            "LANGSMITH_TRACING": "false",
        })

    def cli(self, task, *, transport="mcp", environment=None):
        return subprocess.run(
            [sys.executable, "-m", "workstation_agent", "run", task, "--transport", transport, "--trace"],
            env=environment or self.env, capture_output=True,
            text=True, encoding="utf-8", timeout=30,
        )

    def test_sdk_agent_and_independent_mcp_service(self):
        process = self.cli("读取 hello.txt")
        self.assertEqual(process.returncode, 0, process.stderr)
        response = json.loads(process.stdout)
        self.assertEqual(response["status"], "completed")
        self.assertEqual(response["answer"], "你好，本机服务")
        self.assertEqual([s["kind"] for s in response["steps"] if s["name"] != "context_usage"], ["model", "tool", "model"])
        self.assertIn("langgraph: tools", process.stderr)
        self.assertNotIn("你好，本机服务", process.stderr)
        request = CompatibleAPIHandler.requests[-1]
        self.assertEqual(request["model"], "test-model")
        self.assertEqual(request.get("max_completion_tokens", request.get("max_tokens")), 2048)
        self.assertEqual(response["task_status"]["goal"], "读取 hello.txt")
        assistant = next(m for m in request["messages"] if m["role"] == "assistant")
        tool = next(m for m in request["messages"] if m["role"] == "tool")
        self.assertEqual(assistant["tool_calls"][0]["id"], tool["tool_call_id"])
        self.assertFalse((self.workspace / ".workstation-agent").exists())

    def test_local_transport_uses_same_official_agent(self):
        process = self.cli("读取 hello.txt", transport="local")
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(json.loads(process.stdout)["answer"], "你好，本机服务")

    def test_independent_agent_http_process_uses_sdk_and_mcp(self) -> None:
        """真实启动 Agent 进程，经 HTTP 提交任务，再通过独立 MCP 服务读取文件。"""
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        token = "http_process_test_" + "x" * 32
        environment = {
            **self.env, "WORKSTATION_AGENT_HTTP_PORT": str(port),
            "WORKSTATION_AGENT_HTTP_TOKEN": token,
            "WORKSTATION_AGENT_DATA_DIR": str(self.workspace / "http-state"),
        }
        with TemporaryFile() as output:
            process = subprocess.Popen(
                [sys.executable, "-m", "workstation_agent", "serve-agent"],
                env=environment, stdout=output, stderr=output,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            try:
                with httpx2.Client(
                    base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=5,
                    headers={"Authorization": f"Bearer {token}"},
                ) as client:
                    deadline = time.monotonic() + 20
                    while time.monotonic() < deadline:
                        self.assertIsNone(process.poll(), "Agent 服务提前退出")
                        try:
                            if client.get("/health").status_code == 200:
                                break
                        except httpx2.TransportError:
                            pass
                        time.sleep(0.05)
                    else:
                        self.fail("Agent 服务未就绪")
                    response = client.post("/runs", json={"task": "读取 hello.txt"})
                    self.assertEqual(response.status_code, 202, response.text)
                    run = TypeAdapter(ServiceRun).validate_json(response.content)
                    deadline = time.monotonic() + 20
                    while time.monotonic() < deadline:
                        run = TypeAdapter(ServiceRun).validate_json(client.get(f"/runs/{run.id}").content)
                        if run.status.value == "completed":
                            break
                        time.sleep(0.05)
                    self.assertEqual(run.status.value, "completed", run.error)
                    self.assertEqual(run.answer, "你好，本机服务")
                    self.assertTrue(any(step.name == "read_text_file" for step in run.steps))
                diagnostic = subprocess.run(
                    [sys.executable, "-m", "workstation_agent", "doctor", "--http", "--agent"],
                    env=environment, capture_output=True, text=True, encoding="utf-8", timeout=20,
                )
                self.assertEqual(diagnostic.returncode, 0, diagnostic.stderr)
                self.assertNotIn(token, diagnostic.stdout + diagnostic.stderr)
            finally:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            output.seek(0)
            logs = output.read().decode("utf-8", errors="replace")
            self.assertNotIn(token, logs)
            self.assertNotIn("你好，本机服务", logs)

    def test_remote_parameter_and_path_errors_return_to_model(self):
        for task, code in [("缺失参数", "invalid_arguments"), ("越界", "path_outside_workspace")]:
            with self.subTest(code=code):
                process = self.cli(task)
                self.assertEqual(process.returncode, 0, process.stderr)
                response = json.loads(process.stdout)
                self.assertEqual(response["answer"], code)
                self.assertEqual(response["steps"][1]["status"], "failed")

    def test_provider_failure_is_sanitized(self):
        process = self.cli("HTTP失败")
        self.assertEqual(process.returncode, 1)
        self.assertEqual(json.loads(process.stdout)["status"], "failed")
        self.assertNotIn("private-provider-token", process.stdout + process.stderr)
        self.assertNotIn("Traceback", process.stderr)

    def test_configuration_diagnostics_do_not_expose_keys(self):
        process = subprocess.run(
            [sys.executable, "-m", "workstation_agent", "doctor", "--agent"],
            env=self.env, capture_output=True, text=True, encoding="utf-8", timeout=20,
        )
        self.assertEqual(process.returncode, 0)
        self.assertNotIn(self.env["WORKSTATION_AGENT_API_KEY"], process.stdout)
        self.assertNotIn(self.secret, process.stdout)
        process = self.cli("读取", environment={**self.env, "WORKSTATION_AGENT_API_KEY": "", "OPENAI_API_KEY": ""})
        self.assertEqual(process.returncode, 2)
