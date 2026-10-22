"""限制官方摘要组件的实际请求，授权仍由传入的模型包装器负责。"""

from typing import Any

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

from ..context.budget import ContextLimitExceeded, RequestCounter


class BoundedSummaryModel(BaseChatModel):
    """检查包含摘要提示词的完整输入，并限制摘要输出；不绕过模型授权包装器。"""

    delegate: BaseChatModel = Field(exclude=True, repr=False)
    input_tokens: int
    output_tokens: int

    @property
    def _llm_type(self) -> str:
        return "bounded-summary"

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        """第三方模型要求可扩展关键字参数；仅在计量通过后转交已授权模型。"""
        if RequestCounter(self.delegate).messages(messages) > self.input_tokens:
            raise ContextLimitExceeded("summary_context_limit_exceeded")
        message = self.delegate.invoke(messages, stop=stop, max_tokens=self.output_tokens)
        return ChatResult(generations=[ChatGeneration(message=message)])
