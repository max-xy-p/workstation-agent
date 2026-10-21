"""通过本机 HTTP 管理完整 Agent 任务，路由只调用应用用例。"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Annotated, Literal, TypedDict

import uvicorn
from pydantic import (
    BaseModel, ConfigDict, Field, JsonValue, StringConstraints, TypeAdapter, ValidationError,
)
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.types import ASGIApp

from .. import __version__
from ..application.agent_service import AgentServiceApplication, AgentServiceError, ServiceRun
from ..application.service import AgentApplication
from ..bootstrap import build_agent_application
from ..config import Settings
from ..memory.service import redact
from .access import LocalAccessMiddleware

type ApplicationFactory = Callable[[], AbstractAsyncContextManager[AgentApplication]]
type SessionId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.-]{1,128}$")]
_SESSION = TypeAdapter(SessionId)
_RUN = TypeAdapter(ServiceRun)


class SubmitTask(BaseModel):
    """仅接受任务和会话信息，调用方不能覆盖工作区、模型地址或权限配置。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    task: str = Field(min_length=1, max_length=32_000, pattern=r"\S")
    session_id: SessionId | None = None
    constraints: list[Annotated[str, Field(min_length=1, max_length=2000)]] = Field(
        default_factory=list, max_length=32
    )
    criteria: list[Annotated[str, Field(min_length=1, max_length=2000)]] = Field(
        default_factory=list, max_length=32
    )
    new_task: bool = False


class ResumeTask(BaseModel):
    """提交全部待审批项的决定，授权参数仍取自可信检查点。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    decisions: dict[str, Literal["approve", "reject"]] = Field(min_length=1, max_length=32)


class ServiceState(TypedDict):
    """以官方生命周期状态向路由提供带类型的应用实例。"""

    agent: AgentServiceApplication


async def _parse[T: BaseModel](request: Request[ServiceState], model: type[T]) -> T:
    if request.headers.get("content-type", "").split(";", 1)[0].strip() != "application/json":
        raise HTTPException(415, "请使用 application/json 请求正文")
    try:
        return model.model_validate_json(await request.body())
    except (ValidationError, ValueError, RecursionError):
        # Pydantic 的详细错误包含输入值；HTTP 边界只返回固定说明。
        raise HTTPException(422, "请求 JSON 或参数无效") from None


def _clean(value: JsonValue, secrets: tuple[str | None, ...]) -> JsonValue:
    """返回前清除已知凭据，递归保留 JSON 结构与审批指纹。"""
    if isinstance(value, str):
        return redact(value, secrets)
    if isinstance(value, list):
        return [_clean(item, secrets) for item in value]
    if isinstance(value, dict):
        return {key: _clean(item, secrets) for key, item in value.items()}
    return value


def build_agent_service(
    settings: Settings, *, transport: Literal["local", "mcp"] = "mcp",
    application_factory: ApplicationFactory | None = None,
) -> ASGIApp:
    """组装仅监听本机的 Agent 服务，在生命周期退出前停止全部活动任务。"""
    if not settings.agent_service_token or not settings.workspace.is_dir():
        raise ValueError("Agent 服务需要工作区和 WORKSTATION_AGENT_HTTP_TOKEN")
    if transport == "mcp" and settings.agent_service_port == settings.service_port:
        raise ValueError("Agent HTTP 与 MCP 工具服务需要使用不同端口")
    secrets = (settings.api_key, settings.service_token, settings.agent_service_token, settings.database_url)

    def default_factory() -> AbstractAsyncContextManager[AgentApplication]:
        return build_agent_application(settings, transport=transport, persist_session=True)

    factory = application_factory or default_factory

    @asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[ServiceState]:
        async with factory() as application:
            manager = AgentServiceApplication(application)
            try:
                yield {"agent": manager}
            finally:
                await manager.close()

    def response(value: JsonValue, status: int = 200) -> JSONResponse:
        return JSONResponse(_clean(value, secrets), status_code=status)

    def run_response(run: ServiceRun, status: int = 200) -> JSONResponse:
        value = TypeAdapter(JsonValue).validate_json(_RUN.dump_json(run))
        return response(value, status)

    def session(request: Request[ServiceState]) -> str:
        try:
            return _SESSION.validate_python(request.path_params["session_id"])
        except ValidationError:
            raise HTTPException(422, "会话 ID 无效") from None

    async def health(_request: Request[ServiceState]) -> JSONResponse:
        return response({"status": "ok", "service": "workstation-agent-http", "version": __version__})

    async def submit(request: Request[ServiceState]) -> JSONResponse:
        data = await _parse(request, SubmitTask)
        run = await request.state["agent"].submit(
            data.task, session_id=data.session_id, constraints=tuple(data.constraints),
            criteria=tuple(data.criteria), new_task=data.new_task,
        )
        return run_response(run, 202)

    async def listing(request: Request[ServiceState]) -> JSONResponse:
        runs = request.state["agent"].list_runs()
        value = TypeAdapter(JsonValue).validate_json(TypeAdapter(list[ServiceRun]).dump_json(runs))
        return response(value)

    async def get(request: Request[ServiceState]) -> JSONResponse:
        return run_response(request.state["agent"].get(request.path_params["run_id"]))

    async def cancel(request: Request[ServiceState]) -> JSONResponse:
        accepted = request.state["agent"].cancel(request.path_params["run_id"])
        return response({"cancel_requested": accepted}, 202 if accepted else 200)

    async def approvals(request: Request[ServiceState]) -> JSONResponse:
        pending = await request.state["agent"].approvals(session(request))
        return response({key: value.model_dump(mode="json") for key, value in pending.items()})

    async def resume(request: Request[ServiceState]) -> JSONResponse:
        data = await _parse(request, ResumeTask)
        run = await request.state["agent"].resume(session(request), data.decisions)
        return run_response(run, 202)

    async def service_error(_request: Request, exc: Exception) -> JSONResponse:
        if isinstance(exc, AgentServiceError):
            return response({"error": exc.code, "message": str(exc)}, exc.status)
        if isinstance(exc, HTTPException):
            return response({"error": "invalid_request", "message": str(exc.detail)}, exc.status_code)
        return response({"error": "internal_error", "message": "服务操作失败"}, 500)

    app = Starlette(
        routes=[
            Route("/health", health, methods=["GET"]),
            Route("/runs", submit, methods=["POST"]),
            Route("/runs", listing, methods=["GET"]),
            Route("/runs/{run_id}", get, methods=["GET"]),
            Route("/runs/{run_id}/cancel", cancel, methods=["POST"]),
            Route("/sessions/{session_id}/approvals", approvals, methods=["GET"]),
            Route("/sessions/{session_id}/resume", resume, methods=["POST"]),
        ],
        lifespan=lifespan, max_body_size=65_536,
        exception_handlers={AgentServiceError: service_error, HTTPException: service_error,
                            Exception: service_error},
    )
    return LocalAccessMiddleware(
        app, token=settings.agent_service_token, port=settings.agent_service_port
    )


def run_agent_service(settings: Settings, *, transport: Literal["local", "mcp"] = "mcp") -> None:
    """启动单进程本机 Agent 服务，不启用访问日志或代理地址转发。"""
    uvicorn.run(
        build_agent_service(settings, transport=transport), host="127.0.0.1",
        port=settings.agent_service_port, access_log=False, log_level="critical", proxy_headers=False,
    )
