"""使用官方 MCP 客户端组合内部工具服务与可配置外部服务，不实现协议传输。"""

from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
import os
from hashlib import sha256
from pathlib import Path

import httpx2
from langchain_core.tools import StructuredTool
from mcp import Client
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import Tool

from ..config import Settings
from ..safety.permissions import PermissionLevel
from .builtin.workspace import resolve_inside
from .langchain import make_langchain_tool
from .mcp_config import MCPServerConfig, PERMISSIONS
from .mcp_remote import RemoteMCPTool, public_tool_name, remote_invocation
from .contracts import AsyncTool

type MCPToolExtension = Callable[[Mapping[str, RemoteMCPTool]], Sequence[AsyncTool]]


async def _catalog(client: Client) -> list[Tool]:
    """按官方游标读取目录，拒绝重复工具名和不结束的分页。"""
    tools: list[Tool] = []
    cursor: str | None = None
    seen: set[str] = set()
    for _ in range(32):
        page = await client.list_tools(cursor=cursor)
        tools.extend(page.tools)
        if len(tools) > 4096:
            raise ValueError("MCP 工具目录超过限制")
        cursor = page.next_cursor
        if cursor is None:
            names = [tool.name for tool in tools]
            if len(names) != len(set(names)):
                raise ValueError("MCP 工具目录包含重复名称")
            return tools
        if cursor in seen:
            break
        seen.add(cursor)
    raise ValueError("MCP 工具目录分页无效")


async def _http_client(stack: AsyncExitStack, url: str, token: str | None, timeout: float) -> Client:
    """显式提供 HTTP 客户端，不继承代理、不跟随重定向，凭据只发送到配置地址。"""
    http = await stack.enter_async_context(httpx2.AsyncClient(
        headers={"Authorization": f"Bearer {token}"} if token else {}, trust_env=False,
        follow_redirects=False, timeout=timeout,
    ))
    return await stack.enter_async_context(Client(
        streamable_http_client(url, http_client=http), read_timeout_seconds=timeout,
    ))


@asynccontextmanager
async def local_service_tools(settings: Settings) -> AsyncIterator[list[StructuredTool]]:
    """连接本项目内部 MCP 服务，保留其结果封装、审批元数据和原工具名。"""
    if not settings.service_token:
        raise ValueError("内部 MCP 调用需要配置服务令牌")
    async with AsyncExitStack() as stack:
        client = await _http_client(stack, settings.service_url, settings.service_token, settings.model_timeout)
        catalog = await _catalog(client)
        tools: list[StructuredTool] = []
        for definition in catalog:
            permission = (PermissionLevel.SAFE_READ if definition.annotations and definition.annotations.read_only_hint
                          else PermissionLevel.CONTROLLED_WRITE)
            if definition.name in {"ingest_document", "save_memory_batch"}:
                # 内部服务先读取摄取计划，再校验计划专属写入授权。客户端只授权传输这次请求。
                permission = PermissionLevel.SAFE_READ
            remote = RemoteMCPTool(client, definition, definition.name, permission, internal_service=True,
                                   secrets=settings.secrets)
            tools.append(make_langchain_tool(remote.definition, remote_invocation(remote, settings.tool_output_chars)))
        yield tools


def _stdio_parameters(server: MCPServerConfig, workspace: Path) -> StdioServerParameters:
    """只验证启动配置，不创建进程；供 doctor 与实际连接复用同一规则。"""
    cwd = resolve_inside(workspace, server.cwd)
    if not cwd.is_dir():
        raise ValueError("MCP stdio 工作目录不存在")
    assert server.command is not None
    command = server.command
    if "/" in command or "\\" in command:
        command = str(resolve_inside(workspace, command))
    names = [*server.env, *server.secret_env]
    if any(name not in os.environ for name in names):
        raise ValueError("MCP stdio 缺少配置的环境变量")
    environment = {name: os.environ[name] for name in names}
    return StdioServerParameters(command=command, args=server.args, env=environment, cwd=cwd)


def validate_external_server(server: MCPServerConfig, workspace: Path) -> None:
    """检查凭据变量和 stdio 路径，不连接网络或启动子进程。"""
    server.credential()
    if server.transport == "stdio":
        _stdio_parameters(server, workspace)


async def _external_client(stack: AsyncExitStack, server: MCPServerConfig, workspace: Path,
                           timeout: float) -> tuple[Client, tuple[str, ...]]:
    """只启动可信配置中的进程，工作目录限制在 Agent 工作区内，子进程错误日志不直接输出。"""
    if server.transport == "streamable-http":
        token = server.credential()
        assert server.url is not None
        return await _http_client(stack, server.url, token, timeout), (token,) if token else ()
    parameters = _stdio_parameters(server, workspace)
    error_log = stack.enter_context(open(os.devnull, "w", encoding="utf-8"))
    client = await stack.enter_async_context(Client(stdio_client(parameters, errlog=error_log), read_timeout_seconds=timeout))
    return client, tuple(os.environ[name] for name in server.secret_env)


@asynccontextmanager
async def external_tools(
    settings: Settings, *, extension: MCPToolExtension | None = None,
    hidden: frozenset[str] = frozenset(),
) -> AsyncIterator[list[StructuredTool]]:
    """连接白名单工具，内部适配器可复用不向模型暴露的工具，退出时关闭连接。"""
    stack = AsyncExitStack()
    try:
        tools: list[StructuredTool] = []
        remotes: dict[str, RemoteMCPTool] = {}
        try:
            for server in settings.external_mcp_servers:
                client, credentials = await _external_client(stack, server, settings.workspace, settings.model_timeout)
                catalog = {tool.name: tool for tool in await _catalog(client)}
                if set(server.tools) - catalog.keys():
                    raise ValueError("MCP 配置中的工具不在远端目录中")
                # 可见性单独由 Agent 检查，审批指纹继续绑定连接、权限与远端工具契约。
                approval_config = server.model_dump_json(exclude={"tool_discovery"})
                for name, permission in server.tools.items():
                    remote = RemoteMCPTool(client, catalog[name], public_tool_name(server.name, name), PERMISSIONS[permission],
                                           secrets=(*credentials, *settings.secrets), approval_context=sha256(
                                               (approval_config + catalog[name].model_dump_json()).encode()
                                           ).hexdigest())
                    if remote.definition.name in remotes:
                        raise ValueError("MCP 工具公开名称冲突")
                    remotes[remote.definition.name] = remote
                    if remote.definition.name in hidden:
                        continue
                    tools.append(make_langchain_tool(remote.definition, remote_invocation(remote, settings.tool_output_chars)))
            if extension is not None:
                tools.extend(make_langchain_tool(tool.definition, remote_invocation(tool, settings.tool_output_chars))
                             for tool in extension(remotes))
            names = [tool.name for tool in tools]
            if len(names) != len(set(names)):
                raise ValueError("MCP 工具公开名称冲突")
        except Exception:
            raise ValueError("外部 MCP 初始化失败，请检查连接、工具白名单和凭据配置") from None
        yield tools
    finally:
        # 显式关闭时不向 SDK 任务组转发调用方异常，避免初始化错误被包装成包含内部信息的异常组。
        try:
            await stack.aclose()
        except Exception:
            raise ValueError("外部 MCP 连接关闭失败") from None
