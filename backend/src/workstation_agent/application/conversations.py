"""协调会话和文本历史管理，通过协议访问存储及框架检查点。"""

from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Literal, Protocol

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import CheckpointTuple
from pydantic import TypeAdapter

from ..api.conversations import CreateSession, Page, PageRequest, RenameSession, SessionId, WriteMessage
from ..context.state import RunControlState
from ..domain.conversations import Conversation, ConversationMessage
from ..domain.sources import AnswerSource
from ..domain.runs import Run, RunStatus, new_id
from ..memory.service import redact
from ..safety.permissions import ApprovalPolicy, PermissionLevel
from ..tools.contracts import ToolError
from ..tools.effects import authorized_operation


class ConversationRepository(Protocol):
    """持久化已隔离身份的会话及消息，不负责模型运行和检查点。"""

    def create(self, session_id: str, title: str) -> Conversation: ...
    def get(self, session_id: str) -> Conversation: ...
    def list_sessions(self, page: PageRequest) -> Page[Conversation]: ...
    def rename(self, session_id: str, title: str, revision: int) -> Conversation: ...
    def history(self, session_id: str) -> list[ConversationMessage]: ...
    def list_messages(self, session_id: str, page: PageRequest) -> Page[ConversationMessage]: ...
    def get_message(self, session_id: str, message_id: str) -> ConversationMessage: ...
    def append(
        self, session_id: str, message_id: str, role: Literal["user", "assistant"], content: str,
        run_id: str | None, revision: int | None = None,
        *, sources: list[AnswerSource] | None = None,
    ) -> ConversationMessage: ...
    def edit(self, session_id: str, message: ConversationMessage, content: str, revision: int) -> ConversationMessage: ...
    def truncate(self, session_id: str, position: int, revision: int) -> None: ...
    def delete(self, session_id: str, revision: int) -> None: ...
    def clear(self, session_id: str) -> None: ...


class ConversationCheckpoints(Protocol):
    """使用官方检查点类型检查未结束运行，并与 Agent 共享会话锁。"""

    def config(self, session_id: str) -> RunnableConfig: ...
    def lease(self, config: RunnableConfig, *, deleting: bool = False) -> AbstractContextManager[None]: ...
    def get_tuple(self, config: RunnableConfig) -> CheckpointTuple | None: ...
    def reset_locked(self, config: RunnableConfig) -> None: ...


class ConversationApplication:
    """管理用户可见历史；修改历史先清除检查点，下次运行由保存的文本重新构造上下文。"""

    def __init__(
        self, repository: ConversationRepository, checkpoints: ConversationCheckpoints,
        policy: ApprovalPolicy, *, secrets: tuple[str | None, ...] = (),
    ) -> None:
        self.repository: ConversationRepository = repository
        self.checkpoints: ConversationCheckpoints = checkpoints
        self.policy: ApprovalPolicy = policy
        self.secrets = secrets

    def _read[T](self, operation: Callable[[], T]) -> T:
        return authorized_operation("conversation_read", PermissionLevel.SAFE_READ, self.policy, operation)

    def _write[T](self, operation: Callable[[], T], *, destructive: bool = False) -> T:
        return authorized_operation(
            "conversation_delete" if destructive else "conversation_write",
            PermissionLevel.DESTRUCTIVE if destructive else PermissionLevel.CONTROLLED_WRITE,
            self.policy, operation,
        )

    def _config(self, session_id: str) -> RunnableConfig:
        return self.checkpoints.config(TypeAdapter(SessionId).validate_python(session_id))

    def create(self, data: CreateSession) -> Conversation:
        """创建没有消息和运行记录的新会话，不调用模型。"""
        return self._write(lambda: self.repository.create(new_id("session"), redact(data.title, self.secrets)))

    def get(self, session_id: str) -> Conversation:
        """读取当前身份可见的会话信息。"""
        self._config(session_id)
        return self._read(lambda: self.repository.get(session_id))

    def list_sessions(self, page: PageRequest) -> Page[Conversation]:
        """读取有界的会话列表；不触发数据库初始化。"""
        return self._read(lambda: self.repository.list_sessions(page))

    def rename(self, session_id: str, data: RenameSession) -> Conversation:
        """在会话锁内校验版本并更改标题。"""
        with self.checkpoints.lease(self._config(session_id)):
            return self._write(lambda: self.repository.rename(session_id, redact(data.title, self.secrets), data.revision))

    def list_messages(self, session_id: str, page: PageRequest) -> Page[ConversationMessage]:
        """读取用户文本和最终答复，运行进度仍从运行查询接口取得。"""
        self._config(session_id)
        return self._read(lambda: self.repository.list_messages(session_id, page))

    def get_message(self, session_id: str, message_id: str) -> ConversationMessage:
        """读取明确归属于该会话的一条消息。"""
        self._config(session_id)
        return self._read(lambda: self.repository.get_message(session_id, message_id))

    def _editable(self, session_id: str, config: RunnableConfig, revision: int) -> None:
        current = self.get(session_id)
        if current.revision != revision:
            raise ToolError("会话已变化，请重新读取", code="revision_conflict")
        saved = self.checkpoints.get_tuple(config)
        if saved is not None:
            value = saved.checkpoint["channel_values"].get("run_control")
            if value is not None and not TypeAdapter(RunControlState).validate_python(value)["closed"]:
                raise ToolError("请先结束运行或处理审批", code="session_busy")

    def add_message(self, session_id: str, data: WriteMessage) -> ConversationMessage:
        """只保存用户文本，不触发 Agent；清除检查点使后续运行包含这条文本。"""
        config = self._config(session_id)
        with self.checkpoints.lease(config, deleting=True):
            self._editable(session_id, config, data.revision)
            def save() -> ConversationMessage:
                self.checkpoints.reset_locked(config)
                return self.repository.append(
                    session_id, new_id("message"), "user", redact(data.content, self.secrets), None, data.revision,
                )
            return self._write(save, destructive=True)

    def edit_message(self, session_id: str, message_id: str, data: WriteMessage) -> ConversationMessage:
        """只允许编辑用户文本，并删除其后的全部消息；重新运行需要显式提交任务。"""
        config = self._config(session_id)
        with self.checkpoints.lease(config, deleting=True):
            self._editable(session_id, config, data.revision)
            message = self.get_message(session_id, message_id)
            if message.role != "user":
                raise ToolError("只能编辑用户消息", code="message_read_only")
            def save() -> ConversationMessage:
                self.checkpoints.reset_locked(config)
                return self.repository.edit(session_id, message, redact(data.content, self.secrets), data.revision)
            return self._write(save, destructive=True)

    def delete_message(self, session_id: str, message_id: str, revision: int) -> None:
        """删除选中的消息及其后续文本，防止旧答复继续影响模型。"""
        config = self._config(session_id)
        with self.checkpoints.lease(config, deleting=True):
            self._editable(session_id, config, revision)
            message = self.get_message(session_id, message_id)
            def remove() -> None:
                self.checkpoints.reset_locked(config)
                self.repository.truncate(session_id, message.position, revision)
            self._write(remove, destructive=True)

    def delete(self, session_id: str, revision: int) -> None:
        """删除空闲会话的检查点及文本历史，不删除长期记忆或外部文件。"""
        config = self._config(session_id)
        with self.checkpoints.lease(config, deleting=True):
            self._editable(session_id, config, revision)
            def remove() -> None:
                self.checkpoints.reset_locked(config)
                self.repository.delete(session_id, revision)
            self._write(remove, destructive=True)

    def clear(self, session_id: str) -> None:
        """供已明确授权的 CLI 清除全部历史，包括未完成运行的检查点。"""
        config = self._config(session_id)
        with self.checkpoints.lease(config, deleting=True):
            def remove() -> None:
                self.checkpoints.reset_locked(config)
                self.repository.clear(session_id)
            self._write(remove, destructive=True)

    def start(self, run: Run, *, restore: bool) -> list[ConversationMessage]:
        """由已持有会话锁的 Agent 调用，记录用户输入；无检查点时返回完整文本供重建。"""
        def save() -> list[ConversationMessage]:
            self.repository.create(run.session_id, "新会话")
            message = self.repository.append(
                run.session_id, run.id + "_user", "user", redact(run.task, self.secrets), run.id,
            )
            return self.repository.history(run.session_id) if restore else [message]
        return self._write(save)

    def finish(self, run: Run) -> None:
        """在 Agent 仍持有会话锁时保存最终答复；审批暂停不产生虚构的助手消息。"""
        if run.status == RunStatus.COMPLETED and run.answer is not None:
            self._write(lambda: self.repository.append(
                run.session_id, run.id + "_assistant", "assistant", redact(run.answer or "", self.secrets), run.id,
                sources=run.sources,
            ))
