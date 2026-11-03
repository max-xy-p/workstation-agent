"""验证回答引用只能来自完整检索结果，链接仅指向可信配置中的工作台。"""

import json
import unittest

from langchain_core.messages import ToolMessage
from pydantic import TypeAdapter

from workstation_agent.application.rag import RagApplication
from workstation_agent.application.sources import collect_sources
from workstation_agent.integrations.workstation.config import WorkstationNotesConfig
from workstation_agent.rag.config import RagSettings
from workstation_agent.rag.repository import InMemoryIndexRepository
from workstation_agent.tools.executor import ToolResult


class AnswerSourcesTests(unittest.TestCase):
    def message(self, *, tool: str = "search_documents", truncated: bool = False) -> ToolMessage:
        """使用真实分块和检索构造工具消息，避免用手写证据绕过来源流程。"""
        rag = RagApplication(InMemoryIndexRepository(), RagSettings())
        rag.ingest("workstation://main/workspaces/11/notes/42", "# 数据库\n项目使用 PostgreSQL。",
                   {"source_version": "7"}, title="数据库约定")
        content = json.dumps(rag.search("PostgreSQL", {}, 6), ensure_ascii=False)
        payload = TypeAdapter(ToolResult).dump_json(ToolResult(tool, content[:-4] if truncated else content)).decode()
        return ToolMessage(content=payload, name=tool, tool_call_id="read")

    def test_collects_deduplicated_original_version_and_trusted_link(self) -> None:
        binding = WorkstationNotesConfig(server="main", workspace_id=11, web_url="https://workstation.example.com/")
        message = self.message()
        sources = collect_sources(message, [], resolve_url=binding.note_url)
        self.assertEqual(len(sources), 1)
        source = sources[0]
        self.assertIn("PostgreSQL", source.content)
        self.assertEqual(source.source_version, "7")
        self.assertEqual(source.original_url, "https://workstation.example.com/w/11/notes/42/edit")
        self.assertIn(f"#source-{source.id}", message.content)
        self.assertEqual(collect_sources(message, sources, resolve_url=binding.note_url), sources)

    def test_ignores_other_tools_truncation_errors_and_known_credentials(self) -> None:
        self.assertEqual(collect_sources(self.message(tool="external__search_documents"), []), [])
        self.assertEqual(collect_sources(self.message(truncated=True), []), [])
        self.assertEqual(collect_sources(self.message().model_copy(update={"status": "error"}), []), [])
        self.assertEqual(collect_sources(self.message(), [], secrets=("PostgreSQL",)), [])

    def test_note_links_cannot_change_scope_or_embed_credentials(self) -> None:
        binding = WorkstationNotesConfig(server="main", workspace_id=11, web_url="http://127.0.0.1:5174")
        for uri in ("workstation://other/workspaces/11/notes/42", "workstation://main/workspaces/12/notes/42",
                    "workstation://main/workspaces/11/notes/42?token=secret", "https://untrusted.example/42"):
            self.assertIsNone(binding.note_url(uri))
        for url in ("javascript:alert(1)", "https://user:secret@example.com", "https://example.com/?token=x",
                    "https://example.com/#secret", "https://example.com\\evil", "https://example.com/\n"):
            with self.assertRaises(ValueError):
                WorkstationNotesConfig(server="main", workspace_id=11, web_url=url)
