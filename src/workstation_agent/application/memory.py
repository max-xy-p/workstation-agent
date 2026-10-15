"""应用层受控访问门面，Context 和接口不直接持有数据库。"""

import json

from ..memory.models import MemoryRecord
from ..tools.contracts import ToolError


class MemoryApplication:
    """通过统一工具应用访问记忆，禁止调用方绕过授权执行器。"""

    def __init__(self, tools, *, search_limit=5):
        self.tools = tools
        self.search_limit = search_limit

    def execute(self, name, arguments):
        """统一转换失败，异常中不携带记忆正文。"""
        result = self.tools.execute_tool(name, arguments)
        if not result.success:
            raise ToolError("记忆操作失败", code=result.error_code or "memory_failed")
        return json.loads(result.content)

    def search(self, text, *, limit=None):
        """为 Context 返回已经校验范围的正文记录。"""
        rows = self.execute(
            "search_memory",
            {
                "query": text[:2000],
                "limit": limit or self.search_limit,
            },
        )
        return tuple(MemoryRecord.model_validate(row) for row in rows["records"])
