"""验证官方检查点续聊、身份隔离和动态上下文不持久化。"""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from langchain_core.messages import AIMessage, HumanMessage

from tests.test_agent import ScriptedModel
from workstation_agent.agent.factory import build_agent
from workstation_agent.application import AgentApplication
from workstation_agent.bootstrap import (
    MemoryApprovalPolicy,
    build_memory_application,
    build_session_application,
)
from workstation_agent.config import Settings
from workstation_agent.context import ContextBuilder
from workstation_agent.context.config import ContextSettings
from workstation_agent.memory.config import MemorySettings
from workstation_agent.safety import PermissionRequired
from workstation_agent.storage.sqlite.sessions import SQLiteSessionBackend
from workstation_agent.tools.checkpoints import AuthorizedCheckpointer
from workstation_agent.tools.contracts import ToolError


class SessionIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = Settings(Path(self.temp.name), memory=MemorySettings(project_id="one"))

    def saver(self, settings=None):
        settings = settings or self.settings
        return AuthorizedCheckpointer(
            SQLiteSessionBackend(settings.session_database_path, settings.workspace),
            settings.memory_access,
            MemoryApprovalPolicy(sessions=True),
        )

    async def test_restart_restores_history_and_call_limits_are_per_run(self):
        saver = self.saver()
        app = AgentApplication(
            build_agent(
                ScriptedModel(responses=[AIMessage(content="你好")]),
                [],
                max_model_calls=1,
                checkpointer=saver,
            ),
            checkpointer=saver,
        )
        first = await app.run_task("我是小张", session_id="chat")
        self.assertEqual(first.status.value, "completed")
        restarted = self.saver()
        app = AgentApplication(
            build_agent(
                ScriptedModel(responses=[AIMessage(content="继续")]),
                [],
                max_model_calls=1,
                checkpointer=restarted,
            ),
            checkpointer=restarted,
        )
        second = await app.run_task("继续讨论", session_id="chat")
        self.assertEqual(second.status.value, "completed")
        state = restarted.get_tuple(restarted.config("chat"))
        users = [
            m.content
            for m in state.checkpoint["channel_values"]["messages"]
            if isinstance(m, HumanMessage)
        ]
        self.assertEqual(users, ["我是小张", "继续讨论"])

    async def test_memory_is_ephemeral_and_forgetting_removes_next_model_context(self):
        memory = build_memory_application(self.settings, approve_write=True, approve_delete=True)
        record = memory.execute("remember_memory", {"content": "本项目使用 Python"})
        saver = self.saver()
        model = ScriptedModel(responses=[AIMessage(content="好的")])
        app = AgentApplication(
            build_agent(model, [], checkpointer=saver),
            context=ContextBuilder(memory=memory),
            checkpointer=saver,
        )
        first = await app.run_task("Python 项目", session_id="chat")
        self.assertTrue(any(s.name == "context_sources" for s in first.steps))
        state = saver.get_tuple(saver.config("chat"))
        self.assertNotIn(record["id"], repr(state.checkpoint))
        memory.execute("delete_memory", {"memory_id": record["id"], "expected_version": 1})
        second = await app.run_task("Python 项目", session_id="chat")
        self.assertFalse(any(s.name == "context_sources" for s in second.steps))

    async def test_clear_is_scoped_and_requires_destructive_permission(self):
        from dataclasses import replace

        saver = self.saver()
        app = AgentApplication(
            build_agent(
                ScriptedModel(responses=[AIMessage(content="好的")]),
                [],
                checkpointer=saver,
            ),
            checkpointer=saver,
        )
        await app.run_task("对话", session_id="chat")
        other = replace(self.settings, memory=MemorySettings(project_id="two"))
        build_session_application(other, approve_delete=True).clear("chat")
        self.assertIsNotNone(saver.get_tuple(saver.config("chat")))
        with self.assertRaises(PermissionRequired):
            build_session_application(self.settings).clear("chat")
        build_session_application(self.settings, approve_delete=True).clear("chat")
        self.assertIsNone(saver.get_tuple(saver.config("chat")))

    async def test_tool_output_is_not_persisted_and_known_keys_are_redacted(self):
        from langchain_core.messages import ToolMessage

        from workstation_agent.storage.sqlite.sessions import SessionSerializer

        serializer = SessionSerializer(("private-known-key",))
        values = [
            HumanMessage(content="private-known-key"),
            ToolMessage(
                content="raw-sensitive-output",
                tool_call_id="call",
                artifact={"raw": "other-private-data"},
            ),
        ]
        restored = serializer.loads_typed(serializer.dumps_typed(values))
        self.assertNotIn("private-known-key", repr(restored))
        self.assertNotIn("raw-sensitive-output", repr(restored))
        self.assertIsNone(restored[1].artifact)
        self.assertEqual(restored[1].tool_call_id, "call")

    async def test_busy_session_cannot_be_cleared_by_another_application(self):
        saver = self.saver()
        with saver.lease(saver.config("chat")), self.assertRaises(ToolError) as caught:
            build_session_application(self.settings, approve_delete=True).clear("chat")
        self.assertEqual(caught.exception.code, "session_busy")

    async def test_official_summary_is_authorized_and_retained(self):
        from workstation_agent.models.providers.memory import AuthorizedMemoryModel

        saver = self.saver()
        model = ScriptedModel(responses=[AIMessage(content="摘要或答复")])
        summary = AuthorizedMemoryModel(
            delegate=model, policy=MemoryApprovalPolicy(remote=True), max_calls=1
        )
        app = AgentApplication(
            build_agent(
                model,
                [],
                checkpointer=saver,
                summary_model=summary,
                context_settings=ContextSettings(window=16000, trigger_ratio=0.25),
            ),
            checkpointer=saver,
        )
        for i in range(8):
            run = await app.run_task("讨论项目决策和设计细节" * 10 + str(i), session_id="summary")
            self.assertEqual(run.status.value, "completed", run.error)
        self.assertTrue(summary.calls)

    async def test_bootstrap_wires_persistent_session_and_auto_extraction(self):
        from dataclasses import replace
        from unittest.mock import patch

        from workstation_agent.bootstrap import build_agent_application

        settings = replace(
            self.settings,
            model="offline-test",
            api_key="known-private-key",
            memory=MemorySettings(project_id="one", auto_extract=True, allow_remote=True),
        )
        model = ScriptedModel(
            responses=[
                AIMessage(content="完成"),
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "MemoryCandidate",
                            "id": "extract",
                            "args": {
                                "content": "用户喜欢中文",
                                "evidence": "我喜欢中文",
                                "certain": True,
                            },
                            "type": "tool_call",
                        }
                    ],
                ),
            ]
        )
        with patch(
            "workstation_agent.models.providers.openai.build_chat_model", return_value=model
        ):
            async with build_agent_application(
                settings, transport="local", persist_session=True
            ) as application:
                run = await application.run_task("我喜欢中文", session_id="bootstrap")
        self.assertEqual(run.status.value, "completed")
        self.assertEqual(run.steps[-1].status.value, "completed")
        memory = build_memory_application(settings)
        self.assertEqual(memory.search("喜欢中文")[0].content, "用户喜欢中文")
        self.assertTrue(settings.session_database_path.exists())

    async def test_task_board_survives_restart_and_user_can_start_new_task(self) -> None:
        saver = self.saver()
        app = AgentApplication(
            build_agent(
                ScriptedModel(responses=[AIMessage(content="好的")]), [], checkpointer=saver
            ),
            checkpointer=saver,
        )
        await app.run_task(
            "初始目标", session_id="board", constraints=("只读",), criteria=("列出结果",)
        )
        restarted = self.saver()
        app = AgentApplication(
            build_agent(
                ScriptedModel(responses=[AIMessage(content="继续")]), [], checkpointer=restarted
            ),
            checkpointer=restarted,
        )
        await app.run_task("补充要求", session_id="board", constraints=("使用中文",))
        board = restarted.get_tuple(restarted.config("board")).checkpoint["channel_values"][
            "task_board"
        ]
        self.assertEqual(board["goal"], "初始目标")
        self.assertEqual(board["constraints"], ["只读", "使用中文"])
        self.assertEqual(board["current_request"], "补充要求")
        await app.run_task("新任务", session_id="board", new_task=True)
        board = restarted.get_tuple(restarted.config("board")).checkpoint["channel_values"][
            "task_board"
        ]
        self.assertEqual(board["goal"], "新任务")
        self.assertEqual(board["constraints"], [])
