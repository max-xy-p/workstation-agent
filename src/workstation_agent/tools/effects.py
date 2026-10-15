"""通过统一执行器包裹框架基础设施操作，回调不进入公开工具目录。"""

from .contracts import PydanticTool, ToolArguments, ToolError
from .executor import ToolExecutor
from .registry import ToolRegistry


class _NoArguments(ToolArguments):
    """内部操作的参数已由受控适配器闭包绑定。"""


def authorized_operation(name, permission, policy, callback):
    """仅在权限允许后执行回调，结果对象不序列化到工具日志。"""
    values = []

    class Operation(PydanticTool[_NoArguments]):
        """内部能力不接受模型参数，不能被发现或远程调用。"""

        description = "Internal authorized infrastructure operation."
        arguments_model = _NoArguments

        def run(self, arguments):
            values.append(callback())
            return "ok"

    tool = Operation()
    tool.name, tool.permission = name, permission
    result = ToolExecutor(ToolRegistry((tool,)), policy).execute(name, {})
    if not result.success:
        raise ToolError("内部存储操作失败", code=result.error_code or "storage_failed")
    return values[0]
