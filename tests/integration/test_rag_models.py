"""真实 HTTP 夹具验证官方 SDK 边界，不作为外部模型质量证明。"""

import json
import threading
import unittest
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

from workstation_agent.bootstrap import build_tool_application
from workstation_agent.config import Settings
from workstation_agent.rag.config import RagSettings


class RagAPIHandler(BaseHTTPRequestHandler):
    """返回兼容 embedding 和结构化工具响应，并捕获测试请求。"""

    requests = []
    fail = False

    def log_message(self, *args):
        pass

    def do_POST(self):
        raw = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).requests.append((self.path, raw))
        if type(self).fail:
            self.respond(
                401, {"error": {"message": "private-fixture-token", "type": "authentication_error"}}
            )
            return
        if self.path.endswith("/embeddings"):
            inputs = raw["input"]
            self.respond(
                200,
                {
                    "object": "list",
                    "model": "fixture-embedding",
                    "data": [
                        {"object": "embedding", "index": i, "embedding": [1.0, 0.5]}
                        for i, _ in enumerate(inputs)
                    ],
                    "usage": {"prompt_tokens": 1, "total_tokens": 1},
                },
            )
            return
        name = raw["tools"][0]["function"]["name"]
        if name == "PrefixOutput":
            result = {"prefix": "这段文字描述工具的只读权限。"}
        else:
            payload = json.loads(raw["messages"][-1]["content"])
            result = {"scores": [0.9] * len(payload["candidates"])}
        self.respond(
            200,
            {
                "id": "chatcmpl-fixture",
                "object": "chat.completion",
                "created": 0,
                "model": "fixture-chat",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "rank_fixture",
                                    "type": "function",
                                    "function": {
                                        "name": name,
                                        "arguments": json.dumps(result, ensure_ascii=False),
                                    },
                                }
                            ],
                        },
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )

    def respond(self, status, value):
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class RagModelIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), RagAPIHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

        def close():
            cls.server.shutdown()
            cls.server.server_close()
            cls.thread.join(timeout=5)

        cls.addClassCleanup(close)

    def setUp(self):
        RagAPIHandler.requests = []
        RagAPIHandler.fail = False
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "guide.md").write_text("# 权限\n默认只读。", encoding="utf-8")
        self.settings = Settings(
            self.root,
            api_key="private-test-api-key",
            model_base_url=f"http://127.0.0.1:{self.server.server_port}/v1",
            model_timeout=5,
            rag=RagSettings(
                embedding_model="fixture-embedding", context_model="fixture-chat", allow_remote=True
            ),
        )

    def test_official_sdk_sends_enhanced_text_and_returns_original_evidence(self):
        app = build_tool_application(self.settings, approve_rag_write=True)
        result = app.execute_tool("ingest_document", {"path": "guide.md"})
        self.assertTrue(result.success, result)
        result = app.execute_tool("search_documents", {"query": "工具权限"})
        self.assertTrue(result.success, result)
        payload = json.loads(result.content)
        self.assertEqual(payload["mode"], "hybrid")
        self.assertIn("默认只读", payload["context"])
        self.assertNotIn("这段文字描述", payload["context"])
        embedding = next(
            body for path, body in RagAPIHandler.requests if path.endswith("embeddings")
        )
        self.assertIn("这段文字描述工具", embedding["input"][0])
        self.assertTrue(all(isinstance(value, str) for value in embedding["input"]))
        self.assertTrue(any("tools" in body for _, body in RagAPIHandler.requests))

    def test_remote_error_sanitized_and_does_not_publish(self):
        RagAPIHandler.fail = True
        result = build_tool_application(self.settings, approve_rag_write=True).execute_tool(
            "ingest_document", {"path": "guide.md"}
        )
        self.assertFalse(result.success)
        self.assertNotIn("private-fixture-token", result.content)
        self.assertNotIn("private-test-api-key", result.content)
        self.assertFalse(self.settings.rag_database_path.exists())

    def test_no_authorization_produces_no_http_request(self):
        settings = replace(self.settings, rag=replace(self.settings.rag, allow_remote=False))
        result = build_tool_application(settings, approve_rag_write=True).execute_tool(
            "ingest_document", {"path": "guide.md"}
        )
        self.assertEqual(result.error_code, "permission_required")
        self.assertEqual(RagAPIHandler.requests, [])
