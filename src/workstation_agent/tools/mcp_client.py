"""通过 MCP 连接本机工具服务，把发现的工具交给 LangChain Agent 使用。"""

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx2
from langchain_core.tools import StructuredTool
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from pydantic import TypeAdapter

from ..config import Settings
from .contracts import ToolDefinition
from .executor import ToolResult
from .langchain import make_langchain_tool

_RESULT = TypeAdapter(ToolResult)


@asynccontextmanager
async def workstation_tools(settings: Settings) -> AsyncIterator[list[StructuredTool]]:
    """连接 MCP 服务并包装远程工具，退出时关闭连接。"""
    if not settings.service_token:
        raise ValueError("MCP 调用需要配置服务令牌")
    async with httpx2.AsyncClient(
        headers={"Authorization": f"Bearer {settings.service_token}"},
        trust_env=False, timeout=settings.model_timeout,
    ) as http, Client(
        streamable_http_client(settings.service_url, http_client=http),
        read_timeout_seconds=settings.model_timeout,
    ) as client:
        catalog = await client.list_tools()

        def adapt(tool) -> StructuredTool:
            async def invoke(arguments: dict) -> ToolResult:
                response = await client.call_tool(tool.name, arguments)
                # 严格校验服务端契约；不把异常响应或传输异常原样交给模型。
                result = _RESULT.validate_json(json.dumps(response.structured_content))
                if response.is_error != (not result.success) or result.tool_name != tool.name:
                    raise RuntimeError("工作站返回了不一致的工具结果")
                return result
            return make_langchain_tool(
                ToolDefinition(tool.name, tool.description or tool.name, tool.input_schema), invoke,
            )

        yield [adapt(tool) for tool in catalog.tools]
