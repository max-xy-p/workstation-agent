"""验证文本 CRUD、真实数据库隔离及历史修改后的官方检查点重建。"""

import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from langchain_core.messages import AIMessage

from tests.test_agent import ScriptedModel, call_message
from tests.unit.test_execution_control import CountingTool
from workstation_agent.agent.factory import build_agent
from workstation_agent.api.conversations import CreateSession, PageRequest, RenameSession, WriteMessage
from workstation_agent.application.service import AgentApplication
from workstation_agent.application import ToolApplication
from workstation_agent.bootstrap import (
    MemoryApprovalPolicy, build_conversation_application, build_session_application, database_for,
)
from workstation_agent.config import Settings
from workstation_agent.domain.runs import RunStatus
from workstation_agent.memory.config import MemorySettings
from workstation_agent.safety import AllowSafeReadsPolicy, PermissionRequired
from workstation_agent.storage.postgres.checkpoints import PostgresSessionBackend
from workstation_agent.tools.checkpoints import AuthorizedCheckpointer
from workstation_agent.tools.contracts import ToolError
from workstation_agent.tools.executor import ToolExecutor
from workstation_agent.tools.langchain import local_tools
from workstation_agent.tools.registry import ToolRegistry


class ConversationTests(unittest.IsolatedAsyncioTestCase):
    """在隔离工作区中验证持久历史与官方执行状态的一致性。"""

    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.settings = Settings(Path(directory.name))
        self.history = build_conversation_application(self.settings)
        self.saver = AuthorizedCheckpointer(
            PostgresSessionBackend(database_for(self.settings)), self.settings.memory_access,
            MemoryApprovalPolicy(sessions=True),
        )
        self.app = AgentApplication(
            build_agent(ScriptedModel(responses=[AIMessage(content="原答复"), AIMessage(content="新答复")]),
                        [], checkpointer=self.saver),
            checkpointer=self.saver, history=self.history,
        )

    async def test_edit_truncates_and_next_run_rebuilds_only_retained_text(self) -> None:
        session = self.history.create(CreateSession(title="编辑测试"))
        await self.app.run_task("旧问题", session_id=session.id)
        await self.app.run_task("后续问题", session_id=session.id)
        first = self.history.list_messages(session.id, PageRequest()).items[0]
        self.history.edit_message(session.id, first.id, WriteMessage(
            content="修改后的问题", revision=self.history.get(session.id).revision,
        ))
        self.assertIsNone(self.saver.get_tuple(self.saver.config(session.id)))
        result = await self.app.run_task("请继续", session_id=session.id)
        self.assertEqual(result.status, RunStatus.COMPLETED)
        state = await self.app.graph.aget_state(self.saver.config(session.id))
        contents = [message.content for message in state.values["messages"]]
        self.assertIn("修改后的问题", contents)
        self.assertIn("请继续", contents)
        self.assertNotIn("旧问题", contents)
        self.assertNotIn("后续问题", contents)
        self.assertNotIn("新答复", contents)

    async def test_checkpoint_clear_before_failed_edit_preserves_rebuildable_history(self) -> None:
        result = await self.app.run_task("原问题", session_id="failure")
        with patch.object(self.history.repository, "edit", side_effect=ToolError("写入失败", code="database_failed")):
            with self.assertRaises(ToolError):
                self.history.edit_message("failure", result.id + "_user", WriteMessage(
                    content="未成功的修改", revision=self.history.get("failure").revision,
                ))
        self.assertIsNone(self.saver.get_tuple(self.saver.config("failure")))
        await self.app.run_task("继续", session_id="failure")
        state = await self.app.graph.aget_state(self.saver.config("failure"))
        contents = [message.content for message in state.values["messages"]]
        self.assertIn("原问题", contents)
        self.assertIn("原答复", contents)
        self.assertNotIn("未成功的修改", contents)

    async def test_identity_and_workspace_isolation_and_permission(self) -> None:
        session = self.history.create(CreateSession())
        for settings in (
            replace(self.settings, workspace=self.settings.workspace / "another"),
            replace(self.settings, memory=MemorySettings(project_id="another")),
            replace(self.settings, memory=MemorySettings(user_id="another")),
        ):
            other = build_conversation_application(settings)
            self.assertEqual(other.list_sessions(PageRequest()).items, [])
            with self.assertRaises(ToolError) as caught:
                other.get(session.id)
            self.assertEqual(caught.exception.code, "not_found")
        unapproved = build_conversation_application(self.settings, approve_delete=False)
        with self.assertRaises(PermissionRequired):
            unapproved.delete(session.id, session.revision)
        self.assertEqual(self.history.get(session.id).revision, 0)

    async def test_sql_history_edit_rolls_back_truncation_on_failure(self) -> None:
        result = await self.app.run_task("问题", session_id="rollback")
        repository = self.history.repository
        with patch.object(repository, "_touch", side_effect=ToolError("失败", code="database_failed")):
            with self.assertRaises(ToolError):
                self.history.edit_message("rollback", result.id + "_user", WriteMessage(
                    content="不会保存", revision=self.history.get("rollback").revision,
                ))
        self.assertEqual([message.content for message in self.history.list_messages("rollback", PageRequest()).items],
                         ["问题", "原答复"])

    async def test_cli_clear_cannot_resurrect_saved_messages(self) -> None:
        await self.app.run_task("应被清除", session_id="clear")
        build_session_application(self.settings, approve_delete=True).clear("clear")
        self.assertEqual(self.history.list_messages("clear", PageRequest()).items, [])
        await self.app.run_task("新问题", session_id="clear")
        state = await self.app.graph.aget_state(self.saver.config("clear"))
        self.assertNotIn("应被清除", [message.content for message in state.values["messages"]])

    async def test_session_lease_excludes_other_crud_and_unlocked_reset(self) -> None:
        session = self.history.create(CreateSession())
        with self.saver.lease(self.saver.config(session.id)):
            with self.assertRaises(ToolError) as caught:
                self.history.rename(session.id, RenameSession(title="冲突", revision=0))
            self.assertEqual(caught.exception.code, "session_busy")
        with self.assertRaises(RuntimeError):
            self.saver.reset_locked(self.saver.config(session.id))

    async def test_delete_removes_messages_and_checkpoint_for_only_selected_session(self) -> None:
        await self.app.run_task("待删除", session_id="delete")
        await self.app.run_task("要保留", session_id="keep")
        self.history.delete("delete", self.history.get("delete").revision)
        with self.assertRaises(ToolError) as caught:
            self.history.get("delete")
        self.assertEqual(caught.exception.code, "not_found")
        self.assertIsNone(self.saver.get_tuple(self.saver.config("delete")))
        self.assertEqual(self.history.list_messages("keep", PageRequest()).items[0].content, "要保留")
        database = database_for(self.settings)
        with database.transaction() as connection:
            assert connection is not None
            row = connection.execute(
                "SELECT count(*) FROM workstation.conversation_messages WHERE scope=%s AND session_id=%s",
                (self.settings.memory_access.scope().namespace, "delete"),
            ).fetchone()
            self.assertEqual(row, (0,))

    async def test_preexisting_approval_without_business_history_can_resume(self) -> None:
        tools = ToolApplication(ToolExecutor(ToolRegistry((CountingTool(),)), AllowSafeReadsPolicy()))
        application = AgentApplication(
            build_agent(ScriptedModel(responses=[call_message("count", {}), AIMessage(content="已拒绝")]),
                        local_tools(tools), checkpointer=self.saver),
            checkpointer=self.saver,
        )
        first = await application.run_task("旧版本待审批任务", session_id="preexisting")
        self.assertEqual(first.status, RunStatus.WAITING_APPROVAL)
        with self.assertRaises(ToolError):
            self.history.get("preexisting")
        application.history = self.history
        pending = await application.pending_approvals("preexisting")
        resumed = await application.resume_task("preexisting", {key: "reject" for key in pending})
        self.assertEqual(resumed.status, RunStatus.COMPLETED)
        self.assertEqual(resumed.id, first.id)
        messages = self.history.list_messages("preexisting", PageRequest()).items
        self.assertEqual([message.content for message in messages], ["旧版本待审批任务", "已拒绝"])
