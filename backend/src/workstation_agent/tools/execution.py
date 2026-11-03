"""在工具接入边界传递运行停止信号和一次调用的审批范围。"""

import asyncio
import hashlib
import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from pydantic import BaseModel, ConfigDict, JsonValue

from ..domain.execution import ExecutionControl

EXECUTION: ContextVar[ExecutionControl | None] = ContextVar("execution_control", default=None)


class ToolGrant(BaseModel):
    """用户批准的单次工具及原始参数指纹；不接受模型通过工具参数提交授权。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str
    fingerprint: str

    @classmethod
    def for_call(cls, name: str, arguments: dict[str, JsonValue], *, context: str | None = None) -> "ToolGrant":
        """将原始参数及可选的可信来源上下文规范化后计算指纹，来源不由模型填写。"""
        payload = json.dumps(
            arguments, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        )
        if context is not None:
            payload = json.dumps([context, payload], ensure_ascii=False, separators=(",", ":"))
        return cls(name=name, fingerprint=hashlib.sha256(payload.encode()).hexdigest())


GRANT: ContextVar[ToolGrant | None] = ContextVar("tool_grant", default=None)


@contextmanager
def approved_call(grant: ToolGrant | None) -> Iterator[None]:
    """只在本次调用的上下文内传递授权，退出后立即恢复原值。"""
    token = GRANT.set(grant)
    try:
        yield
    finally:
        GRANT.reset(token)


def check_execution() -> None:
    """供工具在操作前和长循环中检查取消与超时。"""
    control = EXECUTION.get()
    if control is not None:
        control.check()


async def execute_blocking[T](callback: Callable[[], T]) -> T:
    """等待已启动的线程退出后再释放会话锁，避免取消后后台继续写同一会话。"""
    worker = asyncio.create_task(asyncio.to_thread(callback))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        control = EXECUTION.get()
        if control is not None:
            control.cancelled.set()
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                # 重复取消也要等线程收尾；循环结束后由外层 raise 继续传播取消，不能提前释放锁。
                continue
            except Exception:
                break
        # 取出线程异常，避免异常无人读取；原始取消信号仍继续传播。
        if worker.done() and not worker.cancelled():
            worker.exception()
        raise
