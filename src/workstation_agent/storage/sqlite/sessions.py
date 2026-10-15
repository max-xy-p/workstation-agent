"""官方 SQLite Checkpointer 的本机适配，不保存原始工具输出。"""

import sqlite3

from langchain_core.messages import BaseMessage, ToolMessage
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite import SqliteSaver

from ...memory.service import redact
from ...tools.contracts import ToolError


class SessionSerializer:
    """保留框架消息协议，持久化时清除工具正文并脱敏已知秘密。"""

    def __init__(self, secrets=()):
        self.secrets = secrets
        self.serializer = JsonPlusSerializer()

    def _clean(self, value):
        if isinstance(value, ToolMessage):
            return value.model_copy(
                update={
                    "content": "[工具结果未持久化，需要时重新读取]",
                    "artifact": None,
                    "additional_kwargs": {},
                    "response_metadata": {},
                }
            )
        if isinstance(value, BaseMessage):
            fields = self._clean(value.model_dump())
            return type(value).model_validate(fields)
        if isinstance(value, dict):
            return {key: self._clean(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._clean(item) for item in value]
        if isinstance(value, tuple):
            return tuple(self._clean(item) for item in value)
        if isinstance(value, str):
            return redact(value, self.secrets)
        return value

    def dumps_typed(self, value):
        """框架所有 channel 与 pending write 都使用同一脱敏路径。"""
        return self.serializer.dumps_typed(self._clean(value))

    def loads_typed(self, value):
        return self.serializer.loads_typed(value)


class SQLiteSessionBackend:
    """按调用打开官方存储；未初始化时读取不创建数据库。"""

    def __init__(self, path, workspace, *, secrets=()):
        self.path, self.workspace = path.absolute(), workspace.resolve()
        self.serde = SessionSerializer(secrets)

    def acquire(self, thread_id):
        """持有本机进程锁，避免同一会话并发执行或清除后被旧执行重建。"""
        path = self.path.parent / "session-locks" / (thread_id + ".lock")
        if not path.resolve().is_relative_to(self.workspace):
            raise ToolError("会话锁路径越界", code="path_outside_workspace")
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = path.open("a+b")
        try:
            handle.seek(0, 2)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            import os

            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            raise ToolError("会话正在执行，请稍后重试", code="session_busy") from None
        return handle

    def call(self, operation, *args, **kwargs):
        """副作用由 tools/ 中的授权包装器触发，目录限制每次重新检查。"""
        if not self.path.resolve().is_relative_to(self.workspace):
            raise ToolError("会话数据路径越界", code="path_outside_workspace")
        write = operation in {"put", "put_writes", "delete_thread"}
        if not self.path.exists() and not write:
            return [] if operation == "list" else None
        if write:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            self.path if write else self.path.as_uri() + "?mode=ro",
            uri=not write,
            check_same_thread=False,
        )
        try:
            saver = SqliteSaver(connection, serde=self.serde)
            if not write:
                if not connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='checkpoints'"
                ).fetchone():
                    return [] if operation == "list" else None
                saver.is_setup = True
            result = getattr(saver, operation)(*args, **kwargs)
            return list(result) if operation == "list" else result
        finally:
            connection.close()
