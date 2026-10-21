"""通过受控会话接口清除检查点，不把内部格式暴露给 CLI。"""

from typing import Protocol, TypedDict

from langchain_core.runnables import RunnableConfig


class SessionDeletion(Protocol):
    """声明清除用例需要的身份映射与删除能力。"""

    def config(self, session_id: str) -> RunnableConfig: ...
    def delete_thread(self, thread_id: str) -> None: ...


class ClearSessionResult(TypedDict):
    """说明本次清除了哪个会话，长期记忆不属于此操作范围。"""

    cleared: str
    long_term_memory_deleted: bool


class SessionApplication:
    """仅清除当前配置身份下的会话。"""

    def __init__(self, checkpointer: SessionDeletion, *, history: "SessionHistoryDeletion | None" = None) -> None:
        self.checkpointer: SessionDeletion = checkpointer
        self.history: SessionHistoryDeletion | None = history

    def clear(self, session_id: str) -> ClearSessionResult:
        """清除框架历史和摘要，长期记忆需要另行遗忘。"""
        if self.history is not None:
            self.history.clear(session_id)
        else:
            config = self.checkpointer.config(session_id)
            self.checkpointer.delete_thread(config.get("configurable", {})["thread_id"])
        return {"cleared": session_id, "long_term_memory_deleted": False}


class SessionHistoryDeletion(Protocol):
    """声明同时清除用户可见文本及检查点的能力。"""

    def clear(self, session_id: str) -> None: ...
