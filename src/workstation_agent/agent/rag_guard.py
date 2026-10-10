"""在官方 Agent 中间件边界阻止重复的无进展检索。"""

import json

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage


class RagSearchStopped(RuntimeError):
    """表示检索预算之外的重复查询，携带安全的停止原因。"""


class RagSearchGuard(AgentMiddleware):
    """按本次图消息识别重复查询，不在实例中保留跨任务状态。"""

    def _check(self, request):
        seen = set()
        for message in request.state["messages"]:
            if isinstance(message, AIMessage):
                for call in message.tool_calls:
                    if call["name"] == "search_documents":
                        key = json.dumps(call["args"], sort_keys=True, ensure_ascii=False)
                        # 最新请求尚未执行，重复时在工具操作前停止。
                        if key in seen and call["id"] == request.tool_call["id"]:
                            raise RagSearchStopped("重复检索没有新增证据需求")
                        seen.add(key)

    def wrap_tool_call(self, request, handler):
        self._check(request)
        return handler(request)

    async def awrap_tool_call(self, request, handler):
        self._check(request)
        return await handler(request)
