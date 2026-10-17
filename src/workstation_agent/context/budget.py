"""计量系统消息、对话、工具定义和输出约束，并保留计数方式。"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import replace

from langchain.agents.middleware.types import ModelRequest
from langchain.agents.structured_output import (
    AutoStrategy,
    OutputToolBinding,
    ProviderStrategy,
    ToolStrategy,
)
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.messages.utils import (
    MessageLikeRepresentation,
    convert_to_messages,
    count_tokens_approximately,
)
from langchain_core.utils.function_calling import convert_to_openai_tool

from .config import ContextSettings
from .state import ContextRuntime


class ContextLimitExceeded(RuntimeError):
    """必须保留的内容超过预算；调用方应停止，不能发送超限请求。"""


def resolve_settings(settings: ContextSettings, model: BaseChatModel) -> ContextSettings:
    """使用官方模型资料补齐容量，兼容服务应显式配置其真实限制。"""
    profile = model.profile or {}
    return replace(
        settings,
        window=settings.window or profile.get("max_input_tokens"),
        input_limit=settings.input_limit or profile.get("max_input_tokens"),
        output_limit=settings.output_limit or profile.get("max_output_tokens"),
    )


class RequestCounter:
    """优先使用模型的本地计数接口；工具和格式定义始终额外保守计量。"""

    def __init__(self, model: BaseChatModel) -> None:
        self.model = model
        self.method = "model+schema_upper_bound"

    def messages(self, messages: Sequence[BaseMessage]) -> int:
        """计数包含工具参数与结果；无法专用计数时，至少按 UTF-8 字节数预留。"""
        if not messages:
            return 0
        # 本项目只接受文本消息；图像、音频等内容不能沿用文本估算。
        if any(not isinstance(message.content, str) for message in messages):
            raise ContextLimitExceeded("context_unsupported_content")
        try:
            # 基类默认使用 GPT-2，不能当成当前模型的专用计数能力。
            if (
                type(self.model).get_num_tokens_from_messages
                is BaseChatModel.get_num_tokens_from_messages
            ):
                raise NotImplementedError
            return self.model.get_num_tokens_from_messages(list(messages))
        except (NotImplementedError, ValueError, ImportError, OSError):
            self.method = "approximate_utf8_upper_bound"
            approximate = count_tokens_approximately(
                list(messages),
                chars_per_token=1,
                extra_tokens_per_message=12,
            )
            content_bytes = sum(len(message.text.encode("utf-8")) + 12 for message in messages)
            calls = sum(
                len(json.dumps(message.tool_calls, ensure_ascii=False).encode("utf-8"))
                for message in messages
                if isinstance(message, AIMessage)
            )
            return max(approximate, content_bytes + calls)

    def summary_messages(self, messages: Iterable[MessageLikeRepresentation]) -> int:
        """适配官方摘要组件接受的消息格式，在框架边界转换为官方消息对象。"""
        return self.messages(convert_to_messages(messages))

    def schemas(self, request: ModelRequest[ContextRuntime]) -> int:
        """官方 API 负责转换工具；结构化输出经公开策略转换为 JSON Schema。"""
        schemas = [convert_to_openai_tool(tool) for tool in request.tools]
        strategy = request.response_format
        if strategy is not None:
            if isinstance(strategy, ProviderStrategy):
                schemas.append(strategy.to_model_kwargs())
            elif isinstance(strategy, (ToolStrategy, AutoStrategy)):
                # 按官方绑定逐个计入 Union 的输出工具，不能只统计带引用的合并 schema。
                tool_strategy = (
                    strategy
                    if isinstance(strategy, ToolStrategy)
                    else ToolStrategy(strategy.schema)
                )
                schemas.extend(
                    convert_to_openai_tool(OutputToolBinding.from_schema_spec(spec).tool)
                    for spec in tool_strategy.schema_specs
                )
            else:
                raise ContextLimitExceeded("context_unsupported_response_format")
        if not schemas:
            return 0
        # 框架接入边界保留第三方 JSON 类型；业务代码不依赖其中的任意动态字段。
        return len(json.dumps(schemas, ensure_ascii=False).encode("utf-8")) + 32

    def total(self, request: ModelRequest[ContextRuntime], messages: Sequence[BaseMessage]) -> int:
        """在动态内容已经组装后统计完整输入。"""
        system = [request.system_message] if request.system_message else []
        return self.messages([*system, *messages]) + self.schemas(request)
