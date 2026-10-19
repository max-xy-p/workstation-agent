"""通过统一执行器授权内部回调，并保留回调的返回类型。"""

from collections.abc import Callable
from queue import Empty, SimpleQueue

from ..safety.permissions import ApprovalPolicy, PermissionLevel
from .contracts import PydanticTool, ToolArguments, ToolError
from .executor import ToolExecutor
from .registry import ToolRegistry


class _NoArguments(ToolArguments):
    """内部操作的参数由受控调用方绑定，不能通过公开工具指定。"""


def authorized_operation[T](
    name: str,
    permission: PermissionLevel,
    policy: ApprovalPolicy,
    callback: Callable[[], T],
) -> T:
    """权限通过后调用函数；框架对象不序列化到工具日志，返回类型与原回调一致。"""
    values: SimpleQueue[T] = SimpleQueue()

    class Operation(PydanticTool[_NoArguments]):
        """只暴露固定授权操作，不接受业务参数或字符串方法名。"""

        description = "Internal authorized infrastructure operation."
        arguments_model = _NoArguments

        def run(self, arguments: _NoArguments) -> str:
            values.put(callback())
            return "ok"

    tool = Operation()
    tool.name, tool.permission = name, permission
    result = ToolExecutor(ToolRegistry((tool,)), policy).execute(name, {})
    if not result.success:
        raise ToolError("内部操作失败", code=result.error_code or "storage_failed")
    try:
        return values.get_nowait()
    except Empty:
        raise ToolError("内部操作未返回结果", code="invalid_output") from None
