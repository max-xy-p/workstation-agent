"""组装应用与外围适配器，不在构造只读入口时创建索引。"""

from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool
from pydantic import JsonValue

from .application import AgentApplication, ToolApplication
from .application.memory import MemoryApplication
from .application.sessions import SessionApplication
from .application.conversations import ConversationApplication
from .application.runs import RunApplication
from .application.rag import RagApplication
from .application.documents import DocumentApplication
from .config import Settings
from .memory.models import MemoryRecord
from .memory.service import MemoryService
from .rag.backend import RagBackend
from .rag.chunking.contracts import ChunkingStrategy
from .rag.index import IndexSnapshot
from .retrieval.strategy import RetrievalStrategy
from .safety import AllowSafeReadsPolicy
from .safety.permissions import PermissionLevel
from .storage import PostgresDatabase, PostgresRunRepository
from .tools import ToolExecutor, ToolRegistry, default_tools
from .tools.contracts import AsyncTool
from .tools.mcp_remote import RemoteMCPTool, public_tool_name

if TYPE_CHECKING:
    from .application.database import DatabaseApplication


@dataclass(frozen=True, slots=True)
class Infrastructure:
    """保存工具目录和任务存储对象。"""

    tools: ToolRegistry
    runs: PostgresRunRepository


def database_for(settings: Settings) -> PostgresDatabase:
    """按可信工作区身份组装 PostgreSQL 存储，构造时不连接数据库。"""
    return PostgresDatabase(settings.database_url, settings.memory_access.workspace_id)


def build_database_application(settings: Settings) -> "DatabaseApplication":
    """组装显式初始化与检查用例，构造时不连接 PostgreSQL。"""
    from .application.database import DatabaseApplication
    from .storage.postgres.checkpoints import PostgresSessionBackend

    database = database_for(settings)
    return DatabaseApplication(database, PostgresSessionBackend(database))


class RagApprovalPolicy(AllowSafeReadsPolicy):
    """只放行明确授权的 RAG 写入或模型出站，不扩大其他工具权限。"""

    def __init__(self, *, approve_write: bool = False, allow_remote: bool = False) -> None:
        self.approve_write = approve_write
        self.allow_remote = allow_remote

    def allows(
        self, tool_name: str, permission: PermissionLevel, arguments: dict[str, JsonValue]
    ) -> bool:
        return (
            super().allows(tool_name, permission, arguments)
            or (
                self.approve_write
                and tool_name in {"ingest_document", "delete_document", "ingest_external_document"}
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

    def __init__(
        self,
        *,
        write: bool = False,
        delete: bool = False,
        remote: bool = False,
        automatic: bool = False,
        sessions: bool = False,
    ) -> None:
        self.write, self.delete, self.remote = write, delete, remote
        self.automatic, self.sessions = automatic, sessions

    def allows(
        self, tool_name: str, permission: PermissionLevel, arguments: dict[str, JsonValue]
    ) -> bool:
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


def _memory_service(
    settings: Settings, retrieval: RetrievalStrategy[MemoryRecord] | None = None
) -> MemoryService:
    from .memory.retrieval import MemoryRetrievalStrategy
    from .storage.postgres.memory import PostgresMemoryRepository

    return MemoryService(
        PostgresMemoryRepository(
            database_for(settings),
            max_records=settings.memory.max_records,
        ),
        retrieval=retrieval if retrieval is not None else MemoryRetrievalStrategy(),
        max_records=settings.memory.max_records,
        secrets=settings.secrets,
    )


def build_memory_application(
    settings: Settings,
    *,
    approve_write: bool = False,
    approve_delete: bool = False,
    retrieval: RetrievalStrategy[MemoryRecord] | None = None,
) -> "MemoryApplication":
    """构造当前身份下的记忆用例；构造与只读查询不创建数据库。"""
    from .tools.builtin.memory import memory_tools

    if not settings.memory.enabled:
        raise ValueError("记忆未启用")
    tools = memory_tools(_memory_service(settings, retrieval), settings.memory_access)
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


def build_session_application(settings: Settings, *, approve_delete: bool = False) -> SessionApplication:
    """仅组装会话清除入口，不在构造时打开数据库。"""
    from .application.sessions import SessionApplication
    from .storage.postgres.checkpoints import PostgresSessionBackend
    from .tools.checkpoints import AuthorizedCheckpointer

    saver = AuthorizedCheckpointer(
        PostgresSessionBackend(
            database_for(settings),
            secrets=settings.secrets,
        ),
        settings.memory_access,
        MemoryApprovalPolicy(delete=approve_delete),
    )
    return SessionApplication(saver, history=build_conversation_application(settings, approve_delete=approve_delete))


class ConversationPolicy(MemoryApprovalPolicy):
    """只向可信应用调用方授权会话管理，不向模型增加可发现工具。"""

    def __init__(self, *, approve_delete: bool = True) -> None:
        super().__init__(sessions=True, delete=approve_delete)

    def allows(self, tool_name: str, permission: PermissionLevel, arguments: dict[str, JsonValue]) -> bool:
        return super().allows(tool_name, permission, arguments) or (
            tool_name in {"conversation_write", "run_write"} and permission == PermissionLevel.CONTROLLED_WRITE
        ) or (
            self.delete and tool_name in {"conversation_delete", "run_delete"} and permission == PermissionLevel.DESTRUCTIVE
        )


def build_run_application(settings: Settings) -> RunApplication:
    """组装按用户、工作区和项目隔离的运行历史，构造时不访问数据库。"""
    return RunApplication(PostgresRunRepository(
        database_for(settings), scope=settings.memory_access.scope().namespace,
        secrets=settings.secrets,
    ), ConversationPolicy())


def build_conversation_application(settings: Settings, *, approve_delete: bool = True) -> ConversationApplication:
    """组装当前身份的会话管理用例，不连接数据库，也不构造模型。"""
    from .storage.postgres.checkpoints import PostgresSessionBackend
    from .storage.postgres.conversations import PostgresConversationRepository
    from .tools.checkpoints import AuthorizedCheckpointer

    policy = ConversationPolicy(approve_delete=approve_delete)
    secrets = settings.secrets
    return ConversationApplication(
        PostgresConversationRepository(database_for(settings), settings.memory_access.scope().namespace),
        AuthorizedCheckpointer(PostgresSessionBackend(database_for(settings), secrets=secrets), settings.memory_access, policy),
        policy, secrets=secrets,
    )


def build_infrastructure(settings: Settings) -> Infrastructure:
    """创建工具目录和 PostgreSQL 任务存储，不打开数据库连接。"""
    return Infrastructure(
        tools=default_tools(settings.workspace, max_output_chars=settings.tool_output_chars),
        runs=PostgresRunRepository(database_for(settings), scope=settings.memory_access.scope().namespace, secrets=settings.secrets),
    )


def build_rag_application(
    settings: Settings, *, rag_backend: RagBackend | None = None,
    knowledge_retrieval: RetrievalStrategy[IndexSnapshot] | None = None,
    chunking: ChunkingStrategy | None = None,
) -> RagApplication:
    """组合所有摄取入口共用的 RAG 管线，构造时不读取文档或调用外部模型。"""
    from .rag.chunking.structured import StructuredChunkingStrategy
    from .rag.retriever import KnowledgeRetrievalStrategy
    from .storage.postgres.rag import PostgresIndexRepository
    from .tools.builtin.rag import AuthorizedBackend, RagModelTool

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
        PostgresIndexRepository(
            database_for(settings),
            max_chunks=settings.rag.max_chunks,
        ),
        settings.rag,
        backend,
        retrieval=(
            knowledge_retrieval
            if knowledge_retrieval is not None
            else KnowledgeRetrievalStrategy(settings.rag, backend)
        ),
        chunking=chunking if chunking is not None else StructuredChunkingStrategy(settings.rag),
    )
    return rag


def build_document_application(settings: Settings, *, approve_write: bool = False) -> DocumentApplication:
    """组装显式正文摄取用例，任意正文写入工具不进入模型目录。"""
    from .tools.builtin.rag import IngestExternalDocumentTool

    tool = IngestExternalDocumentTool(build_rag_application(settings), secrets=settings.secrets)
    return DocumentApplication(ToolApplication(ToolExecutor(
        ToolRegistry((tool,)), RagApprovalPolicy(approve_write=approve_write),
        max_output_chars=max(settings.tool_output_chars, 4096),
    )))


def build_tool_application(
    settings: Settings,
    *,
    approve_rag_write: bool = False,
    rag_backend: RagBackend | None = None,
    approve_memory_write: bool = False,
    approve_memory_delete: bool = False,
    memory_retrieval: RetrievalStrategy[MemoryRecord] | None = None,
    knowledge_retrieval: RetrievalStrategy[IndexSnapshot] | None = None,
    chunking: ChunkingStrategy | None = None,
) -> ToolApplication:
    """创建统一工具入口，RAG 默认只读，模型出站单独授权。"""
    from .tools.builtin.rag import DeleteDocumentTool, IngestDocumentTool, RagStatusTool, SearchDocumentsTool

    registry = default_tools(settings.workspace, max_output_chars=settings.tool_output_chars)
    tools = [registry.get(definition.name) for definition in registry.definitions]
    rag = build_rag_application(settings, rag_backend=rag_backend,
                                knowledge_retrieval=knowledge_retrieval, chunking=chunking)
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

        tools.extend(
            memory_tools(_memory_service(settings, memory_retrieval), settings.memory_access)
        )

    class Policy(RagApprovalPolicy):
        """仅合并各模块已经明确授权的能力。"""

        def allows(
            self, tool_name: str, permission: PermissionLevel, arguments: dict[str, JsonValue]
        ) -> bool:
            return super().allows(tool_name, permission, arguments) or memory_policy.allows(
                tool_name, permission, arguments
            )

    executor = ToolExecutor(
        ToolRegistry(tuple(tools)),
        Policy(approve_write=approve_rag_write),
        max_output_chars=settings.tool_output_chars,
    )
    return ToolApplication(executor)


def _memory_processors(
    settings: Settings,
    model: BaseChatModel,
    policy: MemoryApprovalPolicy,
    retrieval: RetrievalStrategy[MemoryRecord] | None,
) -> tuple[ToolApplication | None, BaseChatModel | None]:
    """按已授权配置组装独立的提取与摘要入口，不在构造时调用模型。"""
    if not (settings.memory.auto_extract or settings.context.summarize):
        return None, None
    from .models.providers.memory import AuthorizedMemoryModel, LangMemExtractor
    from .tools.extraction import MemoryExtractionTool

    summary_model: BaseChatModel | None = None
    if settings.context.summarize and settings.memory.allow_remote:
        summary_model = AuthorizedMemoryModel(delegate=model, policy=policy, max_calls=1)
    if not settings.memory.auto_extract:
        return None, summary_model
    extractor = LangMemExtractor(model, policy, max_calls=settings.memory.extract_calls)
    operation = MemoryExtractionTool(
        _memory_service(settings, retrieval),
        settings.memory_access,
        extractor,
        settings.memory,
        policy,
    )
    extraction = ToolApplication(ToolExecutor(ToolRegistry((operation,)), policy))
    return extraction, summary_model


@asynccontextmanager
async def build_agent_application(
    settings: Settings,
    *,
    transport: str = "mcp",
    persist_session: bool = False,
    approve_memory_write: bool = False,
    approve_memory_delete: bool = False,
    memory_retrieval: RetrievalStrategy[MemoryRecord] | None = None,
    knowledge_retrieval: RetrievalStrategy[IndexSnapshot] | None = None,
    chunking: ChunkingStrategy | None = None,
) -> AsyncIterator["AgentApplication"]:
    """组装官方 Agent，记忆绑定本机可信身份，外部文件工具仍可走 MCP。"""
    from .agent.factory import build_agent
    from .context import ContextBuilder
    from .models.providers.openai import build_chat_model
    from .tools.langchain import local_tools
    from .tools.mcp_client import local_service_tools, external_tools

    model = build_chat_model(settings)
    memory = (
        build_memory_application(
            settings,
            approve_write=approve_memory_write,
            approve_delete=approve_memory_delete,
            retrieval=memory_retrieval,
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
        from .storage.postgres.checkpoints import PostgresSessionBackend
        from .tools.checkpoints import AuthorizedCheckpointer

        checkpointer = AuthorizedCheckpointer(
            PostgresSessionBackend(
                database_for(settings),
                secrets=settings.secrets,
            ),
            settings.memory_access,
            policy,
        )
    extraction, summary_model = _memory_processors(settings, model, policy, memory_retrieval)

    def application(tools: Sequence[BaseTool]) -> AgentApplication:
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
            context_settings=settings.context,
        )
        return AgentApplication(
            graph,
            repository=build_run_application(settings),
            context=ContextBuilder(memory=memory, max_memory_chars=settings.memory.context_chars),
            checkpointer=checkpointer,
            extraction=extraction,
            run_timeout=settings.run_timeout,
            history=build_conversation_application(settings) if checkpointer is not None else None,
            source_links=settings.workstation_notes.note_url if settings.workstation_notes else None,
            secrets=settings.secrets,
        )

    async with AsyncExitStack() as stack:
        if transport == "mcp":
            tools = await stack.enter_async_context(local_service_tools(settings))
        elif transport == "local":
            tools = local_tools(build_tool_application(
                settings, memory_retrieval=memory_retrieval,
                knowledge_retrieval=knowledge_retrieval, chunking=chunking,
            ))
        else:
            raise ValueError("未知工具传输方式")
        def source_tools(remotes: Mapping[str, RemoteMCPTool]) -> Sequence[AsyncTool]:
            binding = settings.workstation_notes
            if binding is None:
                return ()
            from .integrations.workstation.notes import workstation_note_tools

            remote = remotes.get(public_tool_name(binding.server, "get_note"))
            if remote is None:
                raise ValueError("工作台来源缺少已授权的 get_note 工具")
            return workstation_note_tools(binding, remote, build_document_application(settings, approve_write=True),
                                          max_file_bytes=settings.rag.max_file_bytes, secrets=settings.secrets)

        tools.extend(await stack.enter_async_context(external_tools(settings, extension=source_tools)))
        names = [tool.name for tool in tools]
        if len(names) != len(set(names)):
            raise ValueError("本机与外部 MCP 工具名称冲突")
        yield application(tools)
