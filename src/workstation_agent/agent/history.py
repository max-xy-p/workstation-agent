"""未授权额外模型请求时，使用官方工具裁剪短期历史。"""

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import RemoveMessage, trim_messages
from langchain_core.messages.utils import count_tokens_approximately
from langgraph.graph.message import REMOVE_ALL_MESSAGES


class HistoryTrimMiddleware(AgentMiddleware):
    """保留完整消息协议，避免启用会话后上下文无限增长。"""

    def __init__(self, max_tokens):
        self.max_tokens = max_tokens

    def before_model(self, state, runtime):
        messages = state["messages"]
        kept = trim_messages(
            messages,
            max_tokens=self.max_tokens,
            token_counter=count_tokens_approximately,
            strategy="last",
            start_on="human",
            include_system=True,
            allow_partial=False,
        )
        if not kept and messages:
            raise ValueError("当前消息超过会话上下文预算")
        if len(kept) != len(messages):
            return {"messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES), *kept]}
        return None
