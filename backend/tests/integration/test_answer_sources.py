"""验证图中的检索引用随消息持久化，索引刷新及运行删除不会改写旧回答依据。"""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from langchain_core.messages import AIMessage

from tests.test_agent import ScriptedModel, call_message
from workstation_agent.api.conversations import PageRequest
from workstation_agent.bootstrap import build_agent_application, build_conversation_application, build_rag_application, build_run_application
from workstation_agent.config import Settings
from workstation_agent.domain.runs import RunStatus
from workstation_agent.memory.config import MemorySettings


class PersistedAnswerSourcesTests(unittest.IsolatedAsyncioTestCase):
    async def test_original_evidence_survives_refresh_run_deletion_and_application_restart(self) -> None:
        with TemporaryDirectory() as directory:
            settings = Settings(Path(directory), memory=MemorySettings(enabled=False))
            rag = build_rag_application(settings)
            source_uri = "workstation://notes/workspaces/11/notes/42"
            original = "# 数据库\r\n项目使用 PostgreSQL，保留原始决策。"
            rag.ingest(source_uri, original, {"source_version": "7"}, title="数据库决策")
            evidence = rag.search("PostgreSQL", {}, 6)["evidence"][0]
            from workstation_agent.domain.sources import source_id
            identifier = source_id(evidence["document_id"], evidence["checksum"], evidence["source_start"], evidence["source_end"])
            answer = f"项目使用 PostgreSQL。[数据库决策](#source-{identifier})"
            model = ScriptedModel(responses=[call_message("search_documents", {"query": "PostgreSQL"}), AIMessage(content=answer)])
            with patch("workstation_agent.models.providers.openai.build_chat_model", return_value=model):
                async with build_agent_application(settings, transport="local", persist_session=True) as application:
                    run = await application.run_task("数据库怎么选的", session_id="citation_test")
            self.assertEqual(run.status, RunStatus.COMPLETED, run.error)
            self.assertEqual(run.sources[0].content, evidence["content"])
            self.assertEqual(run.sources[0].source_version, "7")
            history = build_conversation_application(settings)
            messages = history.list_messages(run.session_id, PageRequest()).items
            self.assertEqual(messages[0].sources, [])
            self.assertEqual(messages[1].sources, run.sources)
            self.assertEqual(history.get_message(run.session_id, messages[1].id).sources, run.sources)
            rag.ingest(source_uri, "# 数据库\n新的笔记内容。", {"source_version": "8"}, title="已更新")
            build_run_application(settings).delete(run.id)
            restarted = build_conversation_application(settings)
            saved = restarted.list_messages(run.session_id, PageRequest()).items[1]
            self.assertEqual(saved.content, answer)
            self.assertEqual(saved.sources[0].content, evidence["content"])
            self.assertEqual(saved.sources[0].source_version, "7")
