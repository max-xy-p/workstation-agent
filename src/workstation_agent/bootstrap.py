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


class MemoryApprovalPolicy(AllowSafeReadsPolicy):
    """记忆写入、遗忘、额外出站和会话持久化分别授权。"""

    def __init__(self, *, write=False, delete=False, remote=False, automatic=False, sessions=False):
        self.write, self.delete, self.remote = write, delete, remote
        self.automatic, self.sessions = automatic, sessions

    def allows(self, tool_name, permission, arguments):
        return (
            super().allows(tool_name, permission, arguments)
            or (
                self.write
                and tool_name in {"remember_memory", "update_memory"}
                and permission == PermissionLevel.CONTROLLED_WRITE
            )
            or (
                self.delete
                and tool_name in {"delete_memory", "session_delete_thread", "session_lease_delete"}
                and permission == PermissionLevel.DESTRUCTIVE
            )
            or (
                self.remote
                and tool_name in {"extract_memory", "memory_model"}
                and permission == PermissionLevel.SENSITIVE
            )
            or (
                self.automatic
                and tool_name == "memory_commit_extraction"
                and permission == PermissionLevel.CONTROLLED_WRITE
            )
            or (
                self.sessions
                and tool_name in {"session_put", "session_put_writes", "session_lease"}
                and permission == PermissionLevel.CONTROLLED_WRITE
            )
        )


def _memory_service(settings):
    from .memory.service import MemoryService
    from .storage.sqlite.memory import SQLiteMemoryRepository

    return MemoryService(
        SQLiteMemoryRepository(
            settings.memory_database_path,
            settings.workspace,
            max_records=settings.memory.max_records,
        ),
        max_records=settings.memory.max_records,
        secrets=(settings.api_key, settings.service_token),
    )


def build_memory_application(settings, *, approve_write=False, approve_delete=False):
    """构造当前身份下的记忆用例；构造与只读查询不创建数据库。"""
    from .application.memory import MemoryApplication
    from .tools.builtin.memory import memory_tools

    if not settings.memory.enabled:
        raise ValueError("记忆未启用")
    tools = memory_tools(_memory_service(settings), settings.memory_access)
    policy = MemoryApprovalPolicy(write=approve_write, delete=approve_delete)
    return MemoryApplication(
        ToolApplication(
            ToolExecutor(
                ToolRegistry(tools),
                policy,
                max_output_chars=max(settings.tool_output_chars, 500000),
            )
        ),
        search_limit=settings.memory.search_limit,
    )


def build_session_application(settings, *, approve_delete=False):
    """仅组装会话清除入口，不在构造时打开数据库。"""
    from .application.sessions import SessionApplication
    from .storage.sqlite.sessions import SQLiteSessionBackend
    from .tools.checkpoints import AuthorizedCheckpointer

    saver = AuthorizedCheckpointer(
        SQLiteSessionBackend(
            settings.session_database_path,
            settings.workspace,
            secrets=(settings.api_key, settings.service_token),
        ),
        settings.memory_access,
        MemoryApprovalPolicy(delete=approve_delete),
    )
    return SessionApplication(saver)


def build_infrastructure(settings: Settings) -> Infrastructure:
    """创建工具目录和 SQLite 任务存储。"""
    return Infrastructure(
        tools=default_tools(settings.workspace, max_output_chars=settings.tool_output_chars),
        runs=SQLiteRunRepository(settings.database_path),
    )


def build_tool_application(
    settings: Settings,
    *,
    approve_rag_write: bool = False,
    rag_backend=None,
    approve_memory_write=False,
    approve_memory_delete=False,
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
    memory_policy = MemoryApprovalPolicy(write=approve_memory_write, delete=approve_memory_delete)
    if settings.memory.enabled:
        from .tools.builtin.memory import memory_tools

        tools.extend(memory_tools(_memory_service(settings), settings.memory_access))

    class Policy(RagApprovalPolicy):
        """仅合并各模块已经明确授权的能力。"""

        def allows(self, name, permission, arguments):
            return super().allows(name, permission, arguments) or memory_policy.allows(
                name, permission, arguments
            )

    executor = ToolExecutor(
        ToolRegistry(tuple(tools)),
        Policy(approve_write=approve_rag_write),
        max_output_chars=settings.tool_output_chars,
    )
    return ToolApplication(executor)


@asynccontextmanager
async def build_agent_application(
    settings: Settings,
    *,
    transport="mcp",
    persist_session=False,
    approve_memory_write=False,
    approve_memory_delete=False,
) -> AsyncIterator["AgentApplication"]:
    """组装官方 Agent，记忆绑定本机可信身份，外部文件工具仍可走 MCP。"""
    from .agent.factory import build_agent
    from .context import ContextBuilder
    from .models.providers.openai import build_chat_model
    from .tools.langchain import local_tools
    from .tools.mcp_client import workstation_tools

    model = build_chat_model(settings)
    memory = (
        build_memory_application(
            settings, approve_write=approve_memory_write, approve_delete=approve_memory_delete
        )
        if settings.memory.enabled
        else None
    )
    policy = MemoryApprovalPolicy(
        remote=settings.memory.allow_remote,
        automatic=settings.memory.auto_extract,
        sessions=settings.memory.sessions or persist_session,
    )
    checkpointer = None
    if settings.memory.sessions or persist_session:
        from .storage.sqlite.sessions import SQLiteSessionBackend
        from .tools.checkpoints import AuthorizedCheckpointer

        checkpointer = AuthorizedCheckpointer(
            SQLiteSessionBackend(
                settings.session_database_path,
                settings.workspace,
                secrets=(settings.api_key, settings.service_token),
            ),
            settings.memory_access,
            policy,
        )
    extraction, summary_model = None, None
    if settings.memory.auto_extract or (checkpointer and settings.memory.allow_remote):
        from .models.providers.memory import AuthorizedMemoryModel, LangMemExtractor

        if checkpointer and settings.memory.allow_remote:
            summary_model = AuthorizedMemoryModel(delegate=model, policy=policy, max_calls=1)
        if settings.memory.auto_extract:
            from .tools.extraction import MemoryExtractionTool

            extractor = LangMemExtractor(model, policy, max_calls=settings.memory.extract_calls)
            extraction = ToolApplication(
                ToolExecutor(
                    ToolRegistry(
                        (
                            MemoryExtractionTool(
                                _memory_service(settings),
                                settings.memory_access,
                                extractor,
                                settings.memory,
                                policy,
                            ),
                        )
                    ),
                    policy,
                )
            )

    def application(tools):
        # 本机记忆目录取代远端同名能力，身份不由远端服务或模型参数决定。
        names = {
            "remember_memory",
            "update_memory",
            "delete_memory",
            "get_memory",
            "list_memories",
            "search_memory",
        }
        tools = [tool for tool in tools if tool.name not in names]
        if memory:
            tools.extend(local_tools(memory.tools))
        graph = build_agent(
            model,
            tools,
            max_model_calls=settings.max_steps,
            max_tool_calls=settings.max_tool_calls,
            max_search_calls=settings.rag.max_search_calls,
            checkpointer=checkpointer,
            summary_model=summary_model,
            summary_tokens=settings.memory.summary_tokens,
        )
        return AgentApplication(
            graph,
            context=ContextBuilder(memory=memory, max_memory_chars=settings.memory.context_chars),
            checkpointer=checkpointer,
            extraction=extraction,
        )

    if transport == "mcp":
        async with workstation_tools(settings) as tools:
            yield application(tools)
    elif transport == "local":
        yield application(local_tools(build_tool_application(settings)))
    else:
        raise ValueError("未知工具传输方式")
