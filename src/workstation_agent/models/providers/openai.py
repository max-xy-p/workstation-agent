"""接入 OpenAI 兼容的 Chat Completions 模型接口。"""

from langchain_openai import ChatOpenAI

from ...config import Settings


def build_chat_model(settings: Settings) -> ChatOpenAI:
    """根据配置创建模型客户端，此时不发送请求。"""
    if not settings.model or not settings.api_key:
        raise ValueError("需要配置 WORKSTATION_AGENT_MODEL 和 WORKSTATION_AGENT_API_KEY")
    return ChatOpenAI(
        model=settings.model, api_key=settings.api_key, base_url=settings.model_base_url,
        timeout=settings.model_timeout, max_retries=0,
        use_responses_api=False, stream_usage=False,
    )
