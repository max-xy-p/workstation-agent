"""固定原文证据评估，门槛用于小样例回归，不代表生产正确率。"""

import unittest

from workstation_agent.application.rag import RagApplication
from workstation_agent.rag.config import RagSettings
from workstation_agent.rag.repository import InMemoryIndexRepository

CORPUS = {
    "permissions.md": "# 工具权限\nToolExecutor 默认只读，写入操作需要审批。",
    "routing.md": "# 工具路由\n工具首先交由 ToolExecutor 检查权限。",
    "configuration.md": (
        "# 模型配置\nWORKSTATION_AGENT_MODEL 指定模型名称，API Key 通过环境变量注入。"
    ),
    "storage.md": "# 存储\nSQLite 使用事务发布新版本，失败后保留原来的文档版本。",
}
QUESTIONS = [
    ("ToolExecutor 默认只读", {"permissions.md"}),
    ("写入操作审批", {"permissions.md"}),
    ("工具路由", {"routing.md"}),
    ("WORKSTATION_AGENT_MODEL", {"configuration.md"}),
    ("API Key", {"configuration.md"}),
    ("SQLite 事务发布", {"storage.md"}),
    ("权限检查和写入审批", {"routing.md", "permissions.md"}),
]


class RagEvaluationTests(unittest.TestCase):
    def test_all_required_sources_recalled_at_six_and_citations_grounded(self):
        repository = InMemoryIndexRepository()
        app = RagApplication(repository, RagSettings())
        for source, text in CORPUS.items():
            app.ingest(source, text, {})
        recalled = 0
        reciprocal = 0.0
        for query, required in QUESTIONS:
            with self.subTest(query=query):
                result = app.search(query, {}, 6)
                sources = [item["source_uri"] for item in result["evidence"]]
                self.assertTrue(required.issubset(set(sources)))
                recalled += len(required & set(sources))
                reciprocal += 1 / (min(sources.index(source) for source in required) + 1)
                for item in result["evidence"]:
                    original = CORPUS[item["source_uri"]]
                    self.assertEqual(
                        item["content"], original[item["source_start"] : item["source_end"]]
                    )
        self.assertEqual(recalled / sum(len(required) for _, required in QUESTIONS), 1.0)
        self.assertGreaterEqual(reciprocal / len(QUESTIONS), 0.8)
        self.assertEqual(app.search("NONEXISTENT_ERROR_9876", {}, 6)["status"], "no_evidence")
