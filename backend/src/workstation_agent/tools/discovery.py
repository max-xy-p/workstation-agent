"""在已经授权的目录内查找工具，返回摘要并记录本次运行的可见工具。"""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TypedDict

from langchain.tools import ToolRuntime, tool
from langchain_core.documents import Document
from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool
from langgraph.types import Command
from pydantic import Field, JsonValue, ValidationError

from ..context.state import ContextAgentState, ContextRuntime, DiscoveredTools
from ..rag.index import terms
from ..retrieval.lexical import lexical_search
from ..safety import AllowSafeReadsPolicy, PermissionLevel
from .contracts import ToolArguments, ToolError
from .effects import authorized_operation

FIND_TOOLS = "find_tools"


@dataclass(frozen=True, slots=True)
class DiscoveryEntry:
    """记录已注册工具的主分组、权限和搜索别名。"""

    group: str
    permission: str
    aliases: tuple[str, ...] = ()


class FindToolArguments(ToolArguments):
    """允许浏览根目录或指定分组，也允许带查询直接跳到子组。"""

    group: str | None = Field(default=None, max_length=100)
    query: str = Field(default="", max_length=200)


class ToolSummary(TypedDict):
    """只提供可核对的工具摘要，完整参数由下一轮模型请求提供。"""

    name: str
    group: str
    description: str
    permission: str


class DiscoveryResult(TypedDict):
    """返回分组及少量候选，空结果不会扩大搜索范围。"""

    groups: dict[str, str]
    tools: list[ToolSummary]


class ToolDiscovery:
    """从实际注册的工具构造可查目录，省略空组和未授权工具。"""

    def __init__(
        self, tools: list[BaseTool], entries: Mapping[str, DiscoveryEntry], groups: Mapping[str, str],
    ) -> None:
        self.entries: dict[str, DiscoveryEntry] = {
            item.name: entries[item.name] for item in tools if item.name in entries
        }
        self.summaries: dict[str, ToolSummary] = {
            item.name: {"name": item.name, "group": entry.group, "permission": entry.permission,
                        "description": item.description[:240]}
            for item in tools if (entry := self.entries.get(item.name)) is not None
        }
        self.groups: dict[str, str] = {
            name: description for name, description in groups.items()
            if any(self._within(entry.group, name) for entry in self.entries.values())
        }

    @staticmethod
    def _within(group: str, parent: str) -> bool:
        return group == parent or group.startswith(parent + ".")

    def find(self, arguments: FindToolArguments) -> DiscoveryResult:
        """在指定分组的子树内匹配工具，根目录搜索仅返回相关分组。"""
        group, query = arguments.group, arguments.query.strip()
        if group is not None and group not in self.groups:
            raise ToolError("分组不可用，请先查看可用分组", code="unknown_tool_group")
        children = {name: description for name, description in self.groups.items()
                    if name.rpartition(".")[0] == (group or "")}
        if group is None:
            if query:
                docs = [Document(id=name, page_content=f"{name} {description}") for name, description in children.items()]
                matches = lexical_search(docs, query, 5, terms)
                children = {item.id: children[item.id] for item in matches if item.id is not None}
            return {"groups": children, "tools": []}
        names = [name for name, entry in self.entries.items()
                 if self._within(entry.group, group) and (query or entry.group == group)]
        if query:
            docs = [
                Document(id=name, page_content=" ".join((name, self.summaries[name]["description"], *self.entries[name].aliases)))
                for name in names
            ]
            names = [item.id for item in lexical_search(docs, query, 5, terms) if item.id is not None]
        return {"groups": children, "tools": [self.summaries[name] for name in names[:5]]}

    def as_tool(self) -> BaseTool:
        """使用官方运行时更新检查点中的工具选择。"""
        @tool(FIND_TOOLS, args_schema=FindToolArguments.model_json_schema())
        def find_tools(runtime: ToolRuntime[ContextRuntime, ContextAgentState], **arguments: JsonValue) -> Command:
            """按业务域发现工具。省略 group 查看分组，可用 group 和 query 直接找具体工具。"""
            update: dict[str, object] = {}
            try:
                parsed = FindToolArguments.model_validate(arguments)
                result = authorized_operation(FIND_TOOLS, PermissionLevel.SAFE_READ, AllowSafeReadsPolicy(),
                                              lambda: self.find(parsed))
                if result["tools"] and runtime.context and runtime.context.run_id:
                    selection: DiscoveredTools = {"run_id": runtime.context.run_id,
                                                  "names": [item["name"] for item in result["tools"]]}
                    update["discovered_tools"] = selection
                content, status = json.dumps(result, ensure_ascii=False), "success"
            except (ValidationError, ToolError):
                content, status = "查找参数或分组无效。可先调用 find_tools() 查看可用分组。", "error"
            update["messages"] = [ToolMessage(content, name=FIND_TOOLS,
                                              tool_call_id=runtime.tool_call_id or "", status=status)]
            return Command(update=update)

        return find_tools
