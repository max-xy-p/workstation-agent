"""通过本机 MCP 服务提供工具发现和调用接口。"""

from __future__ import annotations

import asyncio

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
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.types import ASGIApp

from .. import __version__
from ..application.tools import ToolApplication
from ..config import Settings
from ..domain.execution import ExecutionControl
from ..tools import ToolResult
from ..tools.execution import EXECUTION, ToolGrant, approved_call, execute_blocking
from .access import LocalAccessMiddleware

_RESULT = TypeAdapter(ToolResult)


def build_mcp_server(application: ToolApplication) -> Server:
    """将应用的工具列表和调用结果转换为 MCP 格式。"""

    async def list_tools(
        context: ServerRequestContext,
        params: PaginatedRequestParams | None,
    ) -> ListToolsResult:
        catalog = await asyncio.to_thread(application.tool_catalog)
        return ListToolsResult(
            tools=[
                Tool(
                    name=item["name"],
                    description=item["description"],
                    input_schema=item["parameters"],
                    output_schema=_RESULT.json_schema(),
                    annotations=ToolAnnotations(read_only_hint=item["permission"] == "SAFE_READ"),
                )
                for item in catalog
            ]
        )

    async def call_tool(
        context: ServerRequestContext,
        params: CallToolRequestParams,
    ) -> CallToolResult:
        # 在线程中调用应用入口，避免文件读取阻塞 HTTP 请求处理。
        # 审批通过 MCP 元数据传递，不能放入模型可填写的工具参数。
        # HTTP 入口已校验服务令牌；授权仍绑定指定工具与完整原始参数。
        meta = params.meta or {}
        grant_data = meta.get("workstation_approval")
        grant = ToolGrant.model_validate(grant_data) if grant_data is not None else None
        arguments = params.arguments or {}
        if grant is not None and grant != ToolGrant.for_call(params.name, arguments):
            raise ValueError("审批范围与工具请求不符")
        deadline = meta.get("workstation_deadline")
        control = ExecutionControl(float(deadline)) if isinstance(deadline, int | float) else None
        token = EXECUTION.set(control)
        try:
            with approved_call(grant):
                result = await execute_blocking(
                    lambda: application.execute_tool(params.name, arguments)
                )
        finally:
            EXECUTION.reset(token)
        return CallToolResult(
            content=[TextContent(type="text", text=_RESULT.dump_json(result).decode("utf-8"))],
            structured_content=_RESULT.dump_python(result, mode="json"),
            is_error=not result.success,
        )

    return Server(
        "workstation-agent",
        version=__version__,
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )


def build_local_service(application: ToolApplication, settings: Settings) -> ASGIApp:
    """创建仅监听本机的 MCP 工具服务。"""
    if not settings.service_token:
        raise ValueError("WORKSTATION_AGENT_SERVICE_TOKEN is required for serve")
    if not settings.workspace.is_dir():
        raise ValueError("configured workspace must be an existing directory")

    def health(_request: Request) -> JSONResponse:
        """返回不含工作区和配置内容的健康状态。"""
        return JSONResponse(
            {"status": "ok", "service": "workstation-agent", "version": __version__}
        )

    port = settings.service_port
    server = build_mcp_server(application)
    app = server.streamable_http_app(
        stateless_http=True,
        json_response=True,
        max_request_body_size=65_536,
        host="127.0.0.1",
        debug=False,
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
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=settings.service_port,
        access_log=False,
        log_level="warning",
        proxy_headers=False,
    )
