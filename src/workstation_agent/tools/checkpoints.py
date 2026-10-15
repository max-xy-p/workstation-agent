"""把官方 Checkpointer 的基础设施操作接入 ToolExecutor。"""

import asyncio
import hashlib
import re
from contextlib import contextmanager

from langgraph.checkpoint.base import BaseCheckpointSaver

from ..safety import PermissionLevel
from .effects import authorized_operation


class AuthorizedCheckpointer(BaseCheckpointSaver):
    """绑定可信身份命名空间，不向 Agent 暴露裸 Checkpointer。"""

    def __init__(self, backend, access, policy):
        super().__init__(serde=backend.serde)
        self.backend, self.access, self.policy = backend, access, policy

    def config(self, session_id):
        """外部会话名映射为身份隔离的框架 thread_id。"""
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", session_id):
            raise ValueError("会话标识只能包含字母、数字、下划线和连字符")
        namespace = self.access.scope().namespace + ":" + session_id
        return {"configurable": {"thread_id": hashlib.sha256(namespace.encode()).hexdigest()}}

    def _call(self, operation, *args, **kwargs):
        permission = (
            PermissionLevel.DESTRUCTIVE
            if operation == "delete_thread"
            else PermissionLevel.CONTROLLED_WRITE
            if operation in {"put", "put_writes"}
            else PermissionLevel.SAFE_READ
        )
        return authorized_operation(
            "session_" + operation,
            permission,
            self.policy,
            lambda: self.backend.call(operation, *args, **kwargs),
        )

    def get_tuple(self, config):
        return self._call("get_tuple", config)

    def list(self, config, **kwargs):
        yield from self._call("list", config, **kwargs)

    def put(self, config, checkpoint, metadata, new_versions):
        return self._call("put", config, checkpoint, metadata, new_versions)

    def put_writes(self, config, writes, task_id, task_path=""):
        return self._call("put_writes", config, writes, task_id, task_path)

    @contextmanager
    def lease(self, config, *, deleting=False):
        """整个运行和清除互斥，文件锁仅在权限允许后获取。"""
        name = "session_lease_delete" if deleting else "session_lease"
        permission = PermissionLevel.DESTRUCTIVE if deleting else PermissionLevel.CONTROLLED_WRITE
        handle = authorized_operation(
            name,
            permission,
            self.policy,
            lambda: self.backend.acquire(config["configurable"]["thread_id"]),
        )
        try:
            yield
        finally:
            authorized_operation(name, permission, self.policy, handle.close)

    def delete_thread(self, thread_id):
        with self.lease({"configurable": {"thread_id": thread_id}}, deleting=True):
            return self._call("delete_thread", thread_id)

    async def aget_tuple(self, config):
        return await asyncio.to_thread(self.get_tuple, config)

    async def alist(self, config, **kwargs):
        rows = await asyncio.to_thread(lambda: list(self.list(config, **kwargs)))
        for row in rows:
            yield row

    async def aput(self, config, checkpoint, metadata, new_versions):
        return await asyncio.to_thread(self.put, config, checkpoint, metadata, new_versions)

    async def aput_writes(self, config, writes, task_id, task_path=""):
        return await asyncio.to_thread(self.put_writes, config, writes, task_id, task_path)

    async def adelete_thread(self, thread_id):
        return await asyncio.to_thread(self.delete_thread, thread_id)
