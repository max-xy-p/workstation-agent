"""启动相邻仓库的真实 MCP Server，以本机 HTTP 夹具替代认证和业务服务。"""

import json
import os
import unittest
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from unittest.mock import patch

from langchain_core.messages import AIMessage
from pydantic import JsonValue

from tests.test_agent import ScriptedModel, call_message
from tests.workstation_support import note_payload
from workstation_agent.agent.factory import build_agent
from workstation_agent.bootstrap import build_agent_application, build_rag_application
from workstation_agent.config import Settings
from workstation_agent.domain.runs import RunStatus
from workstation_agent.integrations.workstation.config import WorkstationNotesConfig
from workstation_agent.memory.config import MemorySettings
from workstation_agent.tools.mcp_config import MCPServerConfig


class WorkstationServerTests(unittest.IsolatedAsyncioTestCase):
    @unittest.skipUnless(os.getenv("WORKSTATION_MCP_TEST_DIR"), "需要已安装依赖的 Workstation MCP Server 检出目录")
    async def test_real_server_uses_bound_workspace_and_imports_original(self) -> None:
        directory = Path(os.environ["WORKSTATION_MCP_TEST_DIR"]).resolve(strict=True)
        calls: list[str] = []
        original = "# 跨进程验证\r\n这是工作台的原文，关键词是琥珀索引。"
        payload = note_payload(content=original)
        fixture_token = "offline-test-access"

        class Gateway(BaseHTTPRequestHandler):
            """验证 SDK 请求路径及认证头，返回一篇确定版本的笔记。"""

            def _json(self, status: int, value: dict[str, JsonValue]) -> None:
                content = json.dumps(value, ensure_ascii=False).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)

            def do_POST(self) -> None:
                """为真实 MCP 子进程提供测试客户端令牌。"""
                if self.path != "/token":
                    self._json(404, {})
                    return
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
                self._json(200, {"access_token": fixture_token, "expires_in": 300, "token_type": "Bearer"})

            def do_GET(self) -> None:
                """只允许绑定的工作区和笔记 ID。"""
                calls.append(self.path)
                if self.path != "/api/workspaces/11/notes/42" or self.headers.get("Authorization") != f"Bearer {fixture_token}":
                    self._json(404, {})
                    return
                self._json(200, payload)

            def log_message(self, format: str, *args: object) -> None:
                """不记录测试请求头或业务正文。"""

        with TemporaryDirectory() as workspace:
            gateway = ThreadingHTTPServer(("127.0.0.1", 0), Gateway)
            self.addCleanup(gateway.server_close)
            worker = Thread(target=gateway.serve_forever, daemon=True)
            worker.start()
            self.addCleanup(worker.join, 5)
            self.addCleanup(gateway.shutdown)
            url = f"http://127.0.0.1:{gateway.server_port}"
            env = {"WORKSTATION_BASE_URL": url, "WORKSTATION_TOKEN_URL": url + "/token",
                   "WORKSTATION_CLIENT_ID": "offline-test", "WORKSTATION_CLIENT_SECRET": "offline-test-secret",
                   "NO_PROXY": "127.0.0.1"}
            settings = Settings(
                Path(workspace), memory=MemorySettings(enabled=False),
                external_mcp_servers=(MCPServerConfig(
                    name="notes", transport="stdio", command="uv",
                    args=["--directory", str(directory), "run", "--no-sync", "workstation-mcp-server"],
                    env=[key for key in env if key != "WORKSTATION_CLIENT_SECRET"],
                    secret_env=["WORKSTATION_CLIENT_SECRET"], tools={"get_note": "read", "list_notes": "read"},
                    tool_discovery="workstation",
                ),),
                workstation_notes=WorkstationNotesConfig(server="notes", workspace_id=11, expose_get_note=False),
            )
            model = ScriptedModel(responses=[
                call_message("find_tools", {"group": "notes", "query": "list_notes"}),
                call_message("inspect_workstation_note", {"note_id": 42}),
                call_message("ingest_workstation_note", {"note_id": 42, "expected_version": 7}),
                AIMessage(content="已导入"),
            ])
            with patch.dict(os.environ, env), patch(
                "workstation_agent.models.providers.openai.build_chat_model", return_value=model,
            ), patch("workstation_agent.agent.factory.build_agent", wraps=build_agent) as factory:
                async with build_agent_application(settings, transport="local", persist_session=True) as app:
                    names = {tool.name for tool in factory.call_args.args[1]}
                    self.assertEqual({name for name in names if name.startswith("notes__")}, {"notes__list_notes"})
                    self.assertIn("inspect_workstation_note", names)
                    self.assertIn("ingest_workstation_note", names)
                    run = await app.run_task("导入笔记 42")
                    self.assertEqual(run.status, RunStatus.WAITING_APPROVAL, run.error)
                    self.assertEqual(len(calls), 1)
                    assert app.checkpointer is not None
                    saved_state = await app.graph.aget_state(app.checkpointer.config(run.session_id))
                    self.assertEqual(saved_state.values["discovered_tools"]["names"], ["notes__list_notes"])
                    pending = await app.pending_approvals(run.session_id)
                    result = await app.resume_task(run.session_id, {key: "approve" for key in pending})
                    self.assertEqual(result.status, RunStatus.COMPLETED, result.error)
            self.assertEqual(calls, ["/api/workspaces/11/notes/42"] * 2)
            rag = build_rag_application(settings)
            saved = rag.repository.get("workstation://notes/workspaces/11/notes/42")
            assert saved is not None
            self.assertEqual(saved.source.metadata["source_version"], "7")
            self.assertEqual(saved.source.checksum, sha256(original.encode()).hexdigest())
            self.assertEqual(rag.search("琥珀索引", {}, 6)["status"], "ok")
