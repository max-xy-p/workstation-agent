"""定义工具的说明、调用请求和参数检查规则。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Generic, Protocol, TypeVar, runtime_checkable

from pydantic import BaseModel, ConfigDict, ValidationError

from ..safety.permissions import PermissionLevel


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """描述工具名称、用途和参数格式。"""
    # 工具的唯一名称。
    name: str
    # 供使用者和模型查看的用途说明。
    description: str
    # 参数的 JSON Schema，描述类型和必填要求。
    parameters: dict[str, Any]


class ToolError(RuntimeError):
    """表示可安全返回给调用方的已知工具错误。"""

    def __init__(self, message: str, *, code: str = "tool_error") -> None:
        super().__init__(message)
        self.code = code


class Tool(Protocol):
    """约定工具需要提供的说明、权限和调用方法。"""
    @property
    def definition(self) -> ToolDefinition:
        """只读工具说明；既允许实现为属性，也允许实现为实例字段。"""
        ...
    # 执行工具需要的权限等级。
    permission: PermissionLevel

    def invoke(self, arguments: dict[str, Any]) -> str: ...


class ToolRequest(BaseModel):
    """保存一次调用的工具名称和参数。"""
    # 严格检查类型并拒绝多余字段。
    model_config = ConfigDict(strict=True, extra="forbid")
    # 本次要调用的工具名称。
    name: str
    # 本次传入的参数，由执行器进一步校验。
    arguments: dict[str, Any]


class ToolArguments(BaseModel):
    """提供工具参数的公共校验配置。"""
    # 严格类型、禁止多余参数，校验后不可重新赋值字段。
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)


@runtime_checkable
class ValidatedTool(Protocol):
    """约定工具在授权前检查参数并补齐默认值。"""

    def validate_arguments(self, arguments: dict[str, Any]) -> dict[str, Any]: ...


ArgumentsT = TypeVar("ArgumentsT", bound=ToolArguments)


class PydanticTool(Generic[ArgumentsT],ABC):
    """提供参数校验和参数说明生成，子类实现具体操作。"""
    # 工具的唯一名称。
    name: str
    # 工具用途说明。
    description: str
    # 工具需要的权限等级。
    permission: PermissionLevel
    # 用于校验参数并生成参数说明的模型类。
    arguments_model: type[ArgumentsT]

    @property
    def definition(self) -> ToolDefinition:
        """生成工具名称、用途和参数说明。"""
        return ToolDefinition(
            name=self.name,
            description=self.description,
            parameters=self.arguments_model.model_json_schema(),
        )

    def _parse(self, arguments: dict[str, Any]) -> ArgumentsT:
        """校验参数并转换成参数对象。"""
        try:
            return self.arguments_model.model_validate(arguments)
        except ValidationError as exc:
            # 被拒绝的参数值或额外字段名可能包含秘密，不得原样回显。
            issues = set()
            for error in exc.errors(include_input=False, include_context=False, include_url=False):
                field = error["loc"][0] if error["loc"] else "arguments"
                field = field if field in self.arguments_model.model_fields else "arguments"
                issues.add(f"{field} ({error['type']})")
            raise ToolError(
                "invalid arguments: " + ", ".join(sorted(issues)), code="invalid_arguments"
            ) from None

    def validate_arguments(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """校验参数并返回补齐默认值的字典。"""
        return self._parse(arguments).model_dump()

    def invoke(self, arguments: dict[str, Any]) -> str:
        """校验参数后调用 run()；权限由 ToolExecutor 检查。"""
        return self.run(self._parse(arguments))

    @abstractmethod
    def run(self, arguments: ArgumentsT) -> str:
        """实现具体工具操作；应用调用须经过 ToolExecutor。"""
        ...
