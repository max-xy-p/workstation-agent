"""通过官方 Agent 验证显式记忆工具、来源和瞬时上下文。"""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from langchain_core.messages import AIMessage
from pydantic import Field

from tests.postgres_support import has_memory
from tests.test_agent import ScriptedModel, call_message
from workstation_agent.agent.factory import build_agent
from workstation_agent.application import AgentApplication
from workstation_agent.bootstrap import build_memory_application
from workstation_agent.config import Settings
from workstation_agent.context import ContextBuilder
from workstation_agent.tools.langchain import local_tools


class RecordingModel(ScriptedModel):
    """记录实际模型输入，验证召回生效且遗忘后不再注入。"""

    seen: list = Field(default_factory=list)

    async def _agenerate(self, messages, *args, **kwargs):
        self.seen.append(list(messages))
        return await super()._agenerate(messages, *args, **kwargs)


class MemoryAgentScenarios(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_write_has_run_source_and_next_run_recalls_note(self):
        with TemporaryDirectory() as directory:
            settings = Settings(Path(directory))
            memory = build_memory_application(settings, approve_write=True, approve_delete=True)
            model = RecordingModel(
                responses=[
                    call_message("remember_memory", {"content": "本项目使用 Python"}),
                    AIMessage(content="已保存"),
                ]
            )
            app = AgentApplication(
                build_agent(model, local_tools(memory.tools)), context=ContextBuilder(memory=memory)
            )
            first = await app.run_task("记住本项目使用 Python")
            self.assertEqual(first.status.value, "completed")
            record = memory.execute("list_memories", {})["records"][0]
            self.assertEqual(record["sources"][0]["run_id"], first.id)
            model.responses = [AIMessage(content="使用 Python")]
            model.i = 0
            await app.run_task("Python 如何使用")
            self.assertIn(record["id"], repr(model.seen[-1]))
            memory.execute("delete_memory", {"memory_id": record["id"], "expected_version": 1})
            # 新运行没有旧历史，模型循环回复仍可验证第一次输入的记忆消失。
            model.responses = [AIMessage(content="没有相关记忆")]
            await app.run_task("Python 如何使用")
            self.assertNotIn(record["id"], repr(model.seen[-1]))

    async def test_agent_write_denial_does_not_create_database(self):
        with TemporaryDirectory() as directory:
            settings = Settings(Path(directory))
            memory = build_memory_application(settings)
            model = ScriptedModel(
                responses=[call_message("remember_memory", {"content": "使用中文"})]
            )
            run = await AgentApplication(build_agent(model, local_tools(memory.tools))).run_task(
                "记住使用中文"
            )
            self.assertEqual(run.status.value, "waiting_approval")
            self.assertFalse(has_memory(settings))
