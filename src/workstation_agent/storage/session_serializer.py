"""为官方检查点序列化保留消息关系，同时移除工具正文和已知秘密。"""

from dataclasses import fields, is_dataclass, replace
from typing import Any

from langchain_core.messages import BaseMessage, ToolMessage
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from ..memory.service import redact


class SessionSerializer:
    """包装官方通用序列化边界；Any 仅用于框架支持的开放状态类型。"""

    def __init__(self, secrets: tuple[str | None, ...] = ()) -> None:
        self.secrets = secrets
        self.serializer = JsonPlusSerializer()

    def _clean(self, value: Any) -> Any:
        if isinstance(value, ToolMessage):
            return value.model_copy(update={
                "content": "[工具结果未持久化，需要时重新读取]", "artifact": None,
                "additional_kwargs": {}, "response_metadata": {},
            })
        if isinstance(value, BaseMessage):
            return type(value).model_validate(self._clean(value.model_dump()))
        if is_dataclass(value) and not isinstance(value, type):
            return replace(value, **{item.name: self._clean(getattr(value, item.name))
                                     for item in fields(value) if item.init})
        if isinstance(value, dict):
            return {key: self._clean(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._clean(item) for item in value]
        if isinstance(value, tuple):
            return tuple(self._clean(item) for item in value)
        return redact(value, self.secrets) if isinstance(value, str) else value

    def dumps_typed(self, obj: Any) -> tuple[str, bytes]:
        """所有 channel 与中间写入均通过同一脱敏规则。"""
        return self.serializer.dumps_typed(self._clean(obj))

    def loads_typed(self, data: tuple[str, bytes]) -> Any:
        """使用官方序列化器恢复框架状态。"""
        return self.serializer.loads_typed(data)
