from __future__ import annotations

import asyncio
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.language_models.model_profile import ModelProfile

from workstation_agent.agent.factory import build_agent
from workstation_agent.application import AgentApplication
from workstation_agent.bootstrap import build_tool_application
from workstation_agent.config import Settings
from workstation_agent.domain import RunStatus
from workstation_agent.tools.langchain import local_tools


class ScriptedModel(FakeMessagesListChatModel):
    """仅用于离线测试，使用官方消息模型重放确定性回复。"""
    profile: ModelProfile | None = {"max_input_tokens": 32768, "max_output_tokens": 8192}

    def bind_tools(self, tools, **kwargs):
        return self.bind(**kwargs)


def call_message(name="read_text_file", arguments=None):
    return AIMessage(content="", tool_calls=[{
        "name": name, "args": arguments if arguments is not None else {"path": "note.txt"},
        "id": "call_1", "type": "tool_call",
    }])


class AgentTests(unittest.IsolatedAsyncioTestCase):
    async def test_returns_direct_model_answer(self):
        graph = build_agent(ScriptedModel(responses=[AIMessage(content="done")]), [])
        run = await AgentApplication(graph).run_task("回答问题")
        self.assertEqual(run.status, RunStatus.COMPLETED)
        self.assertEqual(run.answer, "done")

    async def test_framework_executes_tool_and_preserves_call_id(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "note.txt").write_text("你好", encoding="utf-8")
            tools = local_tools(build_tool_application(Settings(root)))
            graph = build_agent(ScriptedModel(responses=[call_message(), AIMessage(content="已读取")]), tools)
            state = await graph.ainvoke({"messages": [{"role": "user", "content": "读取文件"}]}, config={"configurable": {"thread_id": "test"}})
            message = next(m for m in state["messages"] if isinstance(m, ToolMessage))
            self.assertEqual(message.tool_call_id, "call_1")
            self.assertEqual(message.artifact["content"], "你好")
            self.assertEqual(json.loads(message.content)["content"], "你好")
            self.assertEqual(state["messages"][-1].content, "已读取")

    async def test_errors_are_tool_messages_that_model_can_correct(self):
        with TemporaryDirectory() as directory:
            tools = local_tools(build_tool_application(Settings(Path(directory))))
            for arguments, code in [({}, "invalid_arguments"), ({"path": 123}, "invalid_arguments"),
                                    ({"path": "../secret"}, "path_outside_workspace")]:
                with self.subTest(code=code):
                    graph = build_agent(ScriptedModel(responses=[call_message(arguments=arguments), AIMessage(content="说明失败")]), tools)
                    state = await graph.ainvoke({"messages": [{"role": "user", "content": "读取文件"}]}, config={"configurable": {"thread_id": "test"}})
                    message = next(m for m in state["messages"] if isinstance(m, ToolMessage))
                    self.assertEqual(message.status, "error")
                    self.assertEqual(json.loads(message.content)["error_code"], code)

    async def test_official_model_limit_stops_repeating_calls(self):
        with TemporaryDirectory() as directory:
            tools = local_tools(build_tool_application(Settings(Path(directory))))
            graph = build_agent(ScriptedModel(responses=[call_message(name="unknown")]), tools, max_model_calls=2)
            run = await AgentApplication(graph).run_task("不断调用工具")
        self.assertEqual(run.status, RunStatus.LIMIT_REACHED)
        self.assertIsNone(run.answer)

    async def test_official_tool_limit_prevents_excess_execution(self):
        with TemporaryDirectory() as directory:
            tools = local_tools(build_tool_application(Settings(Path(directory))))
            graph = build_agent(ScriptedModel(responses=[call_message()]), tools, max_tool_calls=1)
            run = await AgentApplication(graph).run_task("不断读取文件")
            self.assertEqual(run.status, RunStatus.LIMIT_REACHED)
            self.assertEqual(sum(step.kind.value == "tool" for step in run.steps), 1)

    async def test_runs_do_not_share_messages_or_call_counters(self):
        graph = build_agent(ScriptedModel(responses=[AIMessage(content="answer")]), [], max_model_calls=1)
        application = AgentApplication(graph)
        first = await application.run_task("第一条任务")
        second = await application.run_task("第二条任务")
        self.assertEqual(first.status, RunStatus.COMPLETED)
        self.assertEqual(second.status, RunStatus.COMPLETED)
        self.assertNotEqual(first.id, second.id)

    async def test_empty_task_is_rejected(self):
        application = AgentApplication(build_agent(ScriptedModel(responses=[AIMessage(content="answer")]), []))
        with self.assertRaises(ValueError):
            await application.run_task("   ")
