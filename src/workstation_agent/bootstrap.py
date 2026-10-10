"""组装应用与外围适配器，不在构造只读入口时创建索引。"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from .application import AgentApplication, ToolApplication
from .config import Settings
from .safety import AllowSafeReadsPolicy
from .safety.permissions import PermissionLevel
from .storage import SQLiteRunRepository
from .tools import ToolExecutor, ToolRegistry, default_tools


@dataclass(frozen=True, slots=True)
class Infrastructure:
    """保存工具目录和任务存储对象。"""

    tools: ToolRegistry
    runs: SQLiteRunRepository


class RagApprovalPolicy(AllowSafeReadsPolicy):
    """只放行明确授权的 RAG 写入或模型出站，不扩大其他工具权限。"""

    def __init__(self, *, approve_write=False, allow_remote=False):
        self.approve_write = approve_write
        self.allow_remote = allow_remote

    def allows(self, tool_name, permission, arguments):
        return (
            super().allows(tool_name, permission, arguments)
            or (
                self.approve_write
                and tool_name in {"ingest_document", "delete_document"}
                and permission == PermissionLevel.CONTROLLED_WRITE
            )
            or (
                self.allow_remote
                and tool_name == "rag_model"
                and permission == PermissionLevel.SENSITIVE
            )
        )


def build_infrastructure(settings: Settings) -> Infrastructure:
    """创建工具目录和 SQLite 任务存储。"""
    return Infrastructure(
        tools=default_tools(settings.workspace, max_output_chars=settings.tool_output_chars),
        runs=SQLiteRunRepository(settings.database_path),
    )


def build_tool_application(
    settings: Settings, *, approve_rag_write: bool = False, rag_backend=None
) -> ToolApplication:
    """创建统一工具入口，RAG 默认只读，模型出站单独授权。"""
    from .application.rag import RagApplication
    from .storage.sqlite.rag import SQLiteIndexRepository
    from .tools.builtin.rag import (
        AuthorizedBackend,
        DeleteDocumentTool,
        IngestDocumentTool,
        RagModelTool,
        RagStatusTool,
        SearchDocumentsTool,
    )

    registry = default_tools(settings.workspace, max_output_chars=settings.tool_output_chars)
    tools = [registry.get(definition.name) for definition in registry.definitions]
    backend = None
    if settings.rag.embedding_model or settings.rag.context_model:
        if rag_backend is None:
            from .models.providers.rag_openai import OpenAIRagBackend

            rag_backend = OpenAIRagBackend(settings)
        internal = ToolExecutor(
            ToolRegistry((RagModelTool(rag_backend),)),
            RagApprovalPolicy(allow_remote=settings.rag.allow_remote),
            max_output_chars=5_000_000,
        )
        backend = AuthorizedBackend(rag_backend, internal)
    rag = RagApplication(
        SQLiteIndexRepository(
            settings.rag_database_path,
            workspace=settings.workspace,
            max_chunks=settings.rag.max_chunks,
        ),
        settings.rag,
        backend,
    )
    tools.extend(
        (
            IngestDocumentTool(settings.workspace, rag),
            SearchDocumentsTool(rag),
            DeleteDocumentTool(settings.workspace, rag),
            RagStatusTool(rag),
        )
    )
    executor = ToolExecutor(
        ToolRegistry(tuple(tools)),
        RagApprovalPolicy(approve_write=approve_rag_write),
        max_output_chars=settings.tool_output_chars,
    )
    return ToolApplication(executor)


@asynccontextmanager
async def build_agent_application(
    settings: Settings,
    *,
    transport: str = "mcp",
) -> AsyncIterator["AgentApplication"]:
    """组装模型、工具和 Agent 图，并管理 MCP 连接。"""
    from .agent.factory import build_agent
    from .models.providers.openai import build_chat_model
    from .tools.langchain import local_tools
    from .tools.mcp_client import workstation_tools

    model = build_chat_model(settings)

    def application(tools) -> AgentApplication:
        graph = build_agent(
            model,
            tools,
            max_model_calls=settings.max_steps,
            max_tool_calls=settings.max_tool_calls,
            max_search_calls=settings.rag.max_search_calls,
        )
        return AgentApplication(graph)

    if transport == "mcp":
        async with workstation_tools(settings) as tools:
            yield application(tools)
    elif transport == "local":
        yield application(local_tools(build_tool_application(settings)))
    else:
        raise ValueError("未知工具传输方式")
