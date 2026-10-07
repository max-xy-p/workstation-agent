"""通过本机 MCP 服务提供工具发现和调用接口。"""

from __future__ import annotations

import asyncio
import secrets

import uvicorn
from mcp.server import Server, ServerRequestContext
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import (
    CallToolRequestParams,
    CallToolResult,
    ListToolsResult,
    PaginatedRequestParams,
    TextContent,
    Tool,
    ToolAnnotations,
)
from pydantic import TypeAdapter
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from .. import __version__
from ..application.tools import ToolApplication
from ..config import Settings
from ..tools import ToolResult

_RESULT = TypeAdapter(ToolResult)


def build_mcp_server(application: ToolApplication) -> Server:
    """将应用的工具列表和调用结果转换为 MCP 格式。"""
    async def list_tools(
        context: ServerRequestContext, params: PaginatedRequestParams | None,
    ) -> ListToolsResult:
        return ListToolsResult(tools=[
            Tool(
                name=item["name"], description=item["description"],
                input_schema=item["parameters"], output_schema=_RESULT.json_schema(),
                annotations=ToolAnnotations(read_only_hint=item["permission"] == "SAFE_READ"),
            ) for item in application.tool_catalog()
        ])

    async def call_tool(
        context: ServerRequestContext, params: CallToolRequestParams,
    ) -> CallToolResult:
        # 在线程中调用应用入口，避免文件读取阻塞 HTTP 请求处理。
        result = await asyncio.to_thread(
            application.execute_tool, params.name, params.arguments or {},
        )
        return CallToolResult(
            content=[TextContent(type="text", text=_RESULT.dump_json(result).decode("utf-8"))],
            structured_content=_RESULT.dump_python(result, mode="json"),
            is_error=not result.success,
        )

    return Server(
        "workstation-agent", version=__version__, on_list_tools=list_tools,
        on_call_tool=call_tool,
    )


class LocalAccessMiddleware:
    """在解析请求前检查访问地址、来源和认证令牌。"""

    def __init__(self, app: ASGIApp, *, token: str, port: int) -> None:
        self.app = app
        self._credential = ("Bearer " + token).encode("ascii")
        self._hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        self._origins = {f"http://{host}" for host in self._hosts}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            headers = Headers(scope=scope)
            status = None
            if headers.get("host") not in self._hosts:
                status = 421
            elif headers.get("origin") is not None and headers["origin"] not in self._origins:
                status = 403
            elif scope["path"] != "/health":
                credential = headers.get("authorization", "").encode("latin-1")
                if not secrets.compare_digest(credential, self._credential):
                    status = 401
            if status is not None:
                response = JSONResponse({"error": "request denied"}, status_code=status)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def build_local_service(application: ToolApplication, settings: Settings) -> ASGIApp:
    """创建仅监听本机的 MCP 工具服务。"""
    if not settings.service_token:
        raise ValueError("WORKSTATION_AGENT_SERVICE_TOKEN is required for serve")
    if not settings.workspace.is_dir():
        raise ValueError("configured workspace must be an existing directory")

    async def health(request) -> JSONResponse:
        return JSONResponse({"status": "ok", "service": "workstation-agent", "version": __version__})

    port = settings.service_port
    server = build_mcp_server(application)
    app = server.streamable_http_app(
        stateless_http=True, json_response=True, max_request_body_size=65_536,
        host="127.0.0.1", debug=False,
        transport_security=TransportSecuritySettings(
            allowed_hosts=[f"127.0.0.1:{port}", f"localhost:{port}"],
            allowed_origins=[f"http://127.0.0.1:{port}", f"http://localhost:{port}"],
        ),
        custom_starlette_routes=[Route("/health", health, methods=["GET"])],
    )
    return LocalAccessMiddleware(app, token=settings.service_token, port=port)


def run_local_service(application: ToolApplication, settings: Settings) -> None:
    """以前台方式启动本机工具服务。"""
    app = build_local_service(application, settings)
    try:
        uvicorn.run(
            app, host="127.0.0.1", port=settings.service_port,
            access_log=False, log_level="warning", proxy_headers=False,
        )
    except SystemExit as exc:
        # 将服务启动失败统一转换为 CLI 错误。
        if exc.code:
            raise OSError("local service could not start") from None
