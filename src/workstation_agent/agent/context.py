"""通过官方模型调用中间件注入瞬时证据，不写入图消息状态。"""

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import SystemMessage


class ContextMiddleware(AgentMiddleware):
    """只使用应用提供的本次 Context，历史检查点不保存动态记忆副本。"""

    def _request(self, request):
        context = request.runtime.context or {}
        evidence = context.get("evidence", "")
        if not evidence:
            return request
        base = request.system_message.text if request.system_message else ""
        return request.override(system_message=SystemMessage(content=base + "\n\n" + evidence))

    def wrap_model_call(self, request, handler):
        return handler(self._request(request))

    async def awrap_model_call(self, request, handler):
        return await handler(self._request(request))
