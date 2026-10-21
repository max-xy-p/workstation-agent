"""通过本机 HTTP 管理完整 Agent 任务，路由只调用应用用例。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Literal, TypedDict

import uvicorn
from pydantic import (
    BaseModel, JsonValue, TypeAdapter, ValidationError,
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
from ..application.conversations import ConversationApplication
from ..api.conversations import CreateSession, RenameSession, WriteMessage, PageRequest, Page, SessionId
from ..api.runs import SubmitTask, ResumeTask
from ..domain.conversations import Conversation, ConversationMessage
from ..bootstrap import build_agent_application, build_conversation_application
from ..config import Settings
from ..memory.service import redact
from ..tools.contracts import ToolError
from .access import LocalAccessMiddleware

type ApplicationFactory = Callable[[], AbstractAsyncContextManager[AgentApplication]]
_SESSION = TypeAdapter(SessionId)
_RUN = TypeAdapter(ServiceRun)


class ServiceState(TypedDict):
    """以官方生命周期状态向路由提供带类型的应用实例。"""

    agent: AgentServiceApplication
    conversations: ConversationApplication


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
            conversations = build_conversation_application(settings)
            application.history = conversations
            manager = AgentServiceApplication(application)
            try:
                yield {"agent": manager, "conversations": conversations}
            finally:
                await manager.close()

    def response(value: JsonValue, status: int = 200) -> JSONResponse:
        return JSONResponse(_clean(value, secrets), status_code=status)

    def run_response(run: ServiceRun, status: int = 200) -> JSONResponse:
        value = TypeAdapter(JsonValue).validate_json(_RUN.dump_json(run))
        return response(value, status)

    def typed_response[T](value: T, adapter: TypeAdapter[T], status: int = 200) -> JSONResponse:
        return response(TypeAdapter(JsonValue).validate_json(adapter.dump_json(value)), status)

    def page(request: Request[ServiceState]) -> PageRequest:
        try:
            return PageRequest.model_validate(dict(request.query_params))
        except ValidationError:
            raise HTTPException(422, "分页参数无效") from None

    def revision(request: Request[ServiceState]) -> int:
        try:
            value = int(request.query_params["revision"])
            if value < 0 or set(request.query_params) != {"revision"}:
                raise ValueError
            return value
        except (KeyError, ValueError):
            raise HTTPException(422, "请提供非负整数 revision") from None

    async def create_session(request: Request[ServiceState]) -> JSONResponse:
        data = await _parse(request, CreateSession)
        result = await asyncio.to_thread(lambda: request.state["conversations"].create(data))
        return typed_response(result, TypeAdapter(Conversation), 201)

    async def list_sessions(request: Request[ServiceState]) -> JSONResponse:
        data = page(request)
        result = await asyncio.to_thread(lambda: request.state["conversations"].list_sessions(data))
        return typed_response(result, TypeAdapter(Page[Conversation]))

    async def get_session(request: Request[ServiceState]) -> JSONResponse:
        session_id = session(request)
        result = await asyncio.to_thread(lambda: request.state["conversations"].get(session_id))
        return typed_response(result, TypeAdapter(Conversation))

    async def rename_session(request: Request[ServiceState]) -> JSONResponse:
        session_id, data = session(request), await _parse(request, RenameSession)
        result = await asyncio.to_thread(lambda: request.state["conversations"].rename(session_id, data))
        return typed_response(result, TypeAdapter(Conversation))

    async def delete_session(request: Request[ServiceState]) -> JSONResponse:
        session_id, version = session(request), revision(request)
        await asyncio.to_thread(lambda: request.state["conversations"].delete(session_id, version))
        return response({"deleted": session_id})

    async def list_messages(request: Request[ServiceState]) -> JSONResponse:
        session_id, data = session(request), page(request)
        result = await asyncio.to_thread(lambda: request.state["conversations"].list_messages(session_id, data))
        return typed_response(result, TypeAdapter(Page[ConversationMessage]))

    async def get_message(request: Request[ServiceState]) -> JSONResponse:
        session_id, message_id = session(request), request.path_params["message_id"]
        result = await asyncio.to_thread(lambda: request.state["conversations"].get_message(session_id, message_id))
        return typed_response(result, TypeAdapter(ConversationMessage))

    async def add_message(request: Request[ServiceState]) -> JSONResponse:
        session_id, data = session(request), await _parse(request, WriteMessage)
        result = await asyncio.to_thread(lambda: request.state["conversations"].add_message(session_id, data))
        return typed_response(result, TypeAdapter(ConversationMessage), 201)

    async def edit_message(request: Request[ServiceState]) -> JSONResponse:
        session_id, data = session(request), await _parse(request, WriteMessage)
        message_id: str = request.path_params["message_id"]
        result = await asyncio.to_thread(lambda: request.state["conversations"].edit_message(session_id, message_id, data))
        return typed_response(result, TypeAdapter(ConversationMessage))

    async def delete_message(request: Request[ServiceState]) -> JSONResponse:
        session_id, version = session(request), revision(request)
        message_id: str = request.path_params["message_id"]
        await asyncio.to_thread(lambda: request.state["conversations"].delete_message(session_id, message_id, version))
        return response({"deleted": message_id, "following_messages_deleted": True})

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
        if isinstance(exc, ToolError):
            errors = {
                "not_found": (404, "会话或消息不存在"),
                "revision_conflict": (409, "会话已变化，请重新读取后修改"),
                "session_busy": (409, "会话正在运行或等待审批，请先结束运行"),
                "message_read_only": (409, "只能编辑用户消息"),
                "database_not_configured": (503, "数据库未配置"),
                "database_failed": (503, "数据库操作暂不可用"),
            }
            status, message = errors.get(exc.code, (500, "会话操作失败"))
            return response({"error": exc.code if exc.code in errors else "internal_error", "message": message}, status)
        return response({"error": "internal_error", "message": "服务操作失败"}, 500)

    app = Starlette(
        routes=[
            Route("/health", health, methods=["GET"]),
            Route("/sessions", create_session, methods=["POST"]),
            Route("/sessions", list_sessions, methods=["GET"]),
            Route("/sessions/{session_id}", get_session, methods=["GET"]),
            Route("/sessions/{session_id}", rename_session, methods=["PATCH"]),
            Route("/sessions/{session_id}", delete_session, methods=["DELETE"]),
            Route("/sessions/{session_id}/messages", list_messages, methods=["GET"]),
            Route("/sessions/{session_id}/messages", add_message, methods=["POST"]),
            Route("/sessions/{session_id}/messages/{message_id}", get_message, methods=["GET"]),
            Route("/sessions/{session_id}/messages/{message_id}", edit_message, methods=["PATCH"]),
            Route("/sessions/{session_id}/messages/{message_id}", delete_message, methods=["DELETE"]),
            Route("/runs", submit, methods=["POST"]),
            Route("/runs", listing, methods=["GET"]),
            Route("/runs/{run_id}", get, methods=["GET"]),
            Route("/runs/{run_id}/cancel", cancel, methods=["POST"]),
            Route("/sessions/{session_id}/approvals", approvals, methods=["GET"]),
            Route("/sessions/{session_id}/resume", resume, methods=["POST"]),
        ],
        lifespan=lifespan, max_body_size=65_536,
        exception_handlers={AgentServiceError: service_error, HTTPException: service_error, ToolError: service_error,
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
