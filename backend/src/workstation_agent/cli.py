"""提供任务执行、工具调用和配置检查的命令行入口。"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from dataclasses import asdict
from importlib.metadata import version
from typing import Literal

from pydantic import JsonValue, TypeAdapter
from pydantic import __version__ as pydantic_version

from . import __version__
from .bootstrap import build_tool_application
from .config import Settings
from .context.config import ContextConfigurationError
from .context.state import TaskBoard
from .domain.runs import Run
from .tools import ToolResult

_RESULT_ADAPTER = TypeAdapter(ToolResult)


def build_parser() -> argparse.ArgumentParser:
    """定义命令行参数。"""
    parser = argparse.ArgumentParser(prog="workstation-agent")
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    doctor = subparsers.add_parser("doctor", help="check the local agent configuration")
    doctor.add_argument(
        "--rag", action="store_true", help="require configured RAG model authorization"
    )
    doctor.add_argument("--service", action="store_true", help="require MCP service configuration")
    doctor.add_argument(
        "--memory", action="store_true", help="check memory storage and extraction settings"
    )
    doctor.add_argument("--agent", action="store_true", help="check model and MCP configuration")
    doctor.add_argument("--http", action="store_true", help="check independent Agent service configuration")
    doctor.add_argument("--database", action="store_true", help="check the PostgreSQL connection without migrations")
    run = subparsers.add_parser("run", help="run a task with the official LangChain agent")
    run.add_argument("task")
    run.add_argument("--session", help="persist and continue this session ID")
    run.add_argument(
        "--new-task", action="store_true", help="start a new task board in this session"
    )
    run.add_argument("--constraint", action="append", default=[], help="add a user constraint")
    run.add_argument("--criterion", action="append", default=[], help="add a completion criterion")
    run.add_argument("--approve-memory-write", action="store_true")
    run.add_argument("--approve-memory-delete", action="store_true")
    run.add_argument("--transport", choices=("mcp", "local"), default="mcp")
    run.add_argument("--trace", action="store_true", help="print official graph node names")
    approvals = subparsers.add_parser("approvals", help="show pending tool approvals")
    approvals.add_argument("session")
    resume = subparsers.add_parser("resume", help="resume the saved tool approvals")
    resume.add_argument("session")
    resume.add_argument(
        "--decision",
        action="append",
        required=True,
        help="interrupt ID=approve or interrupt ID=reject; repeat for each item",
    )
    confirm = subparsers.add_parser("confirm-status", help="confirm reviewed task items")
    confirm.add_argument("session")
    confirm.add_argument("--task-id", required=True)
    confirm.add_argument("--revision", required=True, type=int)
    confirm.add_argument("--item", action="append", required=True)
    for command in (approvals, resume, confirm):
        command.add_argument("--transport", choices=("mcp", "local"), default="mcp")
    subparsers.add_parser("serve", help="start the local MCP workstation service")
    agent_service = subparsers.add_parser("serve-agent", help="start the local Agent HTTP service")
    agent_service.add_argument("--transport", choices=("mcp", "local"), default="mcp")
    tools = subparsers.add_parser("tools", help="list tools and their argument schemas")
    tools.add_argument("--json", action="store_true", help="print schemas and permissions as JSON")
    tool = subparsers.add_parser("tool", help="execute a tool through validation and permissions")
    tool.add_argument("name", help="registered tool name")
    tool.add_argument(
        "--trace", action="store_true", help="print LangGraph node progress to stderr"
    )
    arguments = tool.add_mutually_exclusive_group()
    arguments.add_argument("--arguments", help="tool arguments as a JSON object")
    arguments.add_argument("--path", help="path argument for a workspace tool")
    rag = subparsers.add_parser("rag", help="ingest, search or delete RAG documents")
    rag_commands = rag.add_subparsers(dest="rag_command", required=True)
    rag_commands.add_parser("status")
    ingest = rag_commands.add_parser("ingest")
    ingest.add_argument("path")
    ingest.add_argument("--metadata", default="{}", help="JSON string labels")
    ingest.add_argument("--approve-write", action="store_true")
    search = rag_commands.add_parser("search")
    search.add_argument("query")
    search.add_argument("--filters", default="{}", help="JSON string labels")
    search.add_argument("--limit", type=int)
    delete = rag_commands.add_parser("delete")
    delete.add_argument("path")
    delete.add_argument("--approve-write", action="store_true")
    memory = subparsers.add_parser("memory", help="manage owned long-term text notes")
    commands = memory.add_subparsers(dest="memory_command", required=True)
    add = commands.add_parser("add")
    add.add_argument("content")
    add.add_argument(
        "--kind", choices=("preference", "decision", "experience"), default="preference"
    )
    add.add_argument("--scope", choices=("user", "workspace", "project"))
    add.add_argument("--expires-at")
    add.add_argument("--approve-write", action="store_true")
    listing = commands.add_parser("list")
    listing.add_argument("--limit", type=int, default=20)
    listing.add_argument("--offset", type=int, default=0)
    get = commands.add_parser("get")
    get.add_argument("memory_id")
    search_memory = commands.add_parser("search")
    search_memory.add_argument("query")
    search_memory.add_argument("--limit", type=int, default=5)
    update = commands.add_parser("update")
    update.add_argument("memory_id")
    update.add_argument("content")
    update.add_argument("--version", type=int, required=True)
    update.add_argument("--expires-at")
    update.add_argument("--approve-write", action="store_true")
    forget = commands.add_parser("delete")
    forget.add_argument("memory_id")
    forget.add_argument("--version", type=int, required=True)
    forget.add_argument("--approve-delete", action="store_true")
    sessions = subparsers.add_parser("session", help="manage persisted conversation history")
    session_commands = sessions.add_subparsers(dest="session_command", required=True)
    clear = session_commands.add_parser("clear")
    clear.add_argument("session_id")
    clear.add_argument("--approve-delete", action="store_true")
    subparsers.add_parser("init-db", help="initialize PostgreSQL business and checkpoint schemas")
    return parser


def _print_result(result: ToolResult) -> int:
    print(_RESULT_ADAPTER.dump_json(result).decode("utf-8"))
    if result.success:
        return 0
    if result.error_code in {"invalid_arguments", "invalid_json", "unknown_tool"}:
        return 2
    if result.error_code == "permission_required":
        return 3
    return 1


def _run_exit_code(status: str | None) -> int:
    if status == "waiting_approval":
        return 3
    if status in {"failed", "cancelled", "limit_reached"}:
        return 1
    return 0


def _doctor(args: argparse.Namespace, settings: Settings) -> int:
    """显示依赖和配置，并执行用户选择的只读健康检查。"""
    print(f"workspace: {settings.workspace}")
    print(f"workspace exists: {settings.workspace.is_dir()}")
    print(f"model: {settings.model or '(not configured)'}")
    print(f"max steps: {settings.max_steps}")
    print(f"PostgreSQL: {'configured' if settings.database_url else '(not configured)'}")
    print(f"tool output chars: {settings.tool_output_chars}")
    print(f"pydantic: {pydantic_version}")
    print(f"langchain: {version('langchain')}")
    print(f"langchain-openai: {version('langchain-openai')}")
    print(f"langchain-text-splitters: {version('langchain-text-splitters')}")
    print(f"langchain-classic: {version('langchain-classic')}")
    print(f"markdown-it-py: {version('markdown-it-py')}")
    print(f"rank-bm25: {version('rank-bm25')}")
    print(f"langgraph: {version('langgraph')}")
    print(f"mcp: {version('mcp')}")
    print(f"service endpoint: {settings.service_url}")
    print(f"service token: {'configured' if settings.service_token else '(not configured)'}")
    print(f"Agent HTTP endpoint: {settings.agent_service_url}")
    print(f"Agent HTTP token: {'configured' if settings.agent_service_token else '(not configured)'}")
    ready = settings.workspace.is_dir() and (not args.service or bool(settings.service_token))
    print(f"model base URL: {settings.model_base_url}")
    print(f"model API key: {'configured' if settings.api_key else '(not configured)'}")
    print(f"model timeout: {settings.model_timeout}")
    print(f"run timeout (including approval wait): {settings.run_timeout}")
    print(f"Context task board token cap: {settings.context.board_tokens}")
    print(f"max tool calls: {settings.max_tool_calls}")
    ready = _doctor_rag(args, settings) and ready
    ready = _doctor_memory(args, settings) and ready
    if args.database:
        from .bootstrap import build_database_application
        try:
            connected = build_database_application(settings).check()
        except Exception:
            connected = False
        print(f"PostgreSQL connection: {'ok' if connected else 'unavailable'}")
        ready = connected and ready
    if args.agent or args.http:
        ready = _doctor_agent(settings, require_mcp=args.agent) and ready
    if args.http:
        ready = bool(settings.agent_service_token) and ready
        print("Agent HTTP sessions: enabled by serve-agent")
        if settings.agent_service_port == settings.service_port:
            print("Agent HTTP and MCP services require distinct ports when used together")
            if args.agent:
                ready = False
    return 0 if ready else 1


def _doctor_rag(args: argparse.Namespace, settings: Settings) -> bool:
    """检查索引路径和可选模型授权；诊断不摄取或修改文档。"""
    ready = True
    print(f"RAG mode: {'hybrid' if settings.rag.embedding_model else 'bm25'}")
    print(f"RAG contextual model: {settings.rag.context_model or '(structural prefix only)'}")
    print(f"RAG remote authorized: {settings.rag.allow_remote}")
    print(f"RAG chunk size / overlap: {settings.rag.chunk_size} / {settings.rag.chunk_overlap}")
    print(f"RAG context budget (UTF-8 upper bound): {settings.rag.context_budget}")
    print("RAG storage: PostgreSQL, workspace partition")
    if args.rag and (settings.rag.embedding_model or settings.rag.context_model):
        ready = ready and bool(settings.api_key and settings.rag.allow_remote)
    if args.rag:
        try:
            health = build_tool_application(settings).execute_tool("rag_status", {})
        except (ValueError, OSError):
            print("RAG index check failed")
            return False
        if health.success:
            report = json.loads(health.content)
            print(
                f"RAG index: {report['status']}; documents: {report['documents']}; "
                f"chunks: {report['chunks']}"
            )
            ready = ready and report["status"] == "ready"
        else:
            print("RAG index check failed")
            ready = False
    return ready


def _doctor_memory(args: argparse.Namespace, settings: Settings) -> bool:
    """显示记忆与上下文配置，仅在要求检查记忆时读取存储。"""
    ready = True
    print(f"Memory enabled: {settings.memory.enabled}")
    print(
        f"Memory identity: {settings.memory.user_id}; "
        f"project: {settings.memory.project_id or '(workspace)'}"
    )
    print(
        f"Memory context chars / limit: {settings.memory.context_chars} / "
        f"{settings.memory.search_limit}"
    )
    print(f"Memory record budget: {settings.memory.max_records}")
    print(
        f"Memory auto extraction / outbound: {settings.memory.auto_extract} / "
        f"{settings.memory.allow_remote}"
    )
    print(
        f"Memory extraction chars / candidates / calls: {settings.memory.extract_chars} / "
        f"{settings.memory.extract_limit} / {settings.memory.extract_calls}"
    )
    print(f"Sessions enabled: {settings.memory.sessions}")
    print(
        "Context window: "
        f"{settings.context.window or '(model profile or explicit config required)'}"
    )
    print(
        f"Context output / safety tokens: {settings.context.output_tokens} / "
        f"{settings.context.safety_tokens}"
    )
    print(f"Context summary enabled: {settings.context.summarize}")
    if settings.context.summarize and not settings.memory.allow_remote:
        print("Context summary unavailable: MEMORY_ALLOW_REMOTE authorization required")
    print(f"langgraph-checkpoint-postgres: {version('langgraph-checkpoint-postgres')}")
    print(f"langmem: {version('langmem')}")
    try:
        print("Memory storage: PostgreSQL, workspace and scope partitions")
        print("Session storage: official PostgreSQL checkpointer")
        if args.memory and settings.memory.enabled:
            from .bootstrap import build_memory_application

            report = build_memory_application(settings).execute("list_memories", {"limit": 1})
            print(f"Memory records in allowed scopes: {report['total']}")
    except Exception:
        print("Memory storage check failed")
        ready = False
    if settings.memory.auto_extract:
        ready = ready and bool(settings.memory.allow_remote and settings.model and settings.api_key)
    return ready


def _doctor_agent(settings: Settings, *, require_mcp: bool = True) -> bool:
    """校验模型、连接和上下文容量，不发送探测请求。"""
    ready = True
    ready = ready and bool(settings.model and settings.api_key and settings.database_url)
    if require_mcp:
        ready = ready and bool(settings.service_token)
    try:
        from .context.budget import resolve_settings
        from .models.providers.openai import build_chat_model

        budget = resolve_settings(settings.context, build_chat_model(settings)).input_budget()
        print(f"Context input budget: {budget}")
    except ValueError:
        print("Context budget unavailable: configure CONTEXT_WINDOW and check output limits")
        ready = False
    return ready


def _rag(args: argparse.Namespace, settings: Settings) -> int:
    """解析摄取、检索或删除参数，通过工具应用检查权限后执行。"""
    try:
        application = build_tool_application(
            settings, approve_rag_write=getattr(args, "approve_write", False)
        )
        if args.rag_command == "status":
            arguments, name = {}, "rag_status"
        elif args.rag_command == "ingest":
            arguments = {"path": args.path, "metadata": json.loads(args.metadata)}
            name = "ingest_document"
        elif args.rag_command == "delete":
            arguments, name = {"path": args.path}, "delete_document"
        else:
            arguments = {
                "query": args.query,
                "filters": json.loads(args.filters),
                "limit": args.limit if args.limit is not None else settings.rag.search_limit,
            }
            name = "search_documents"
        return _print_result(application.execute_tool(name, arguments))
    except (ValueError, OSError):
        print("invalid RAG configuration or JSON; check settings", file=sys.stderr)
        return 2


def _memory(args: argparse.Namespace, settings: Settings) -> int:
    """把记忆管理命令转换成统一工具调用，不直接操作数据库。"""
    from .bootstrap import build_memory_application

    try:
        memory = build_memory_application(
            settings,
            approve_write=getattr(args, "approve_write", False),
            approve_delete=getattr(args, "approve_delete", False),
        )
        operation = args.memory_command
        if operation == "add":
            name, arguments = (
                "remember_memory",
                {
                    "content": args.content,
                    "kind": args.kind,
                    "scope": args.scope,
                    "expires_at": args.expires_at,
                },
            )
        elif operation == "update":
            name, arguments = (
                "update_memory",
                {
                    "memory_id": args.memory_id,
                    "content": args.content,
                    "expected_version": args.version,
                    "expires_at": args.expires_at,
                },
            )
        elif operation == "delete":
            name, arguments = (
                "delete_memory",
                {
                    "memory_id": args.memory_id,
                    "expected_version": args.version,
                },
            )
        elif operation == "get":
            name, arguments = "get_memory", {"memory_id": args.memory_id}
        elif operation == "list":
            name, arguments = "list_memories", {"limit": args.limit, "offset": args.offset}
        else:
            name, arguments = "search_memory", {"query": args.query, "limit": args.limit}
        return _print_result(memory.tools.execute_tool(name, arguments))
    except Exception:
        print("memory configuration or operation failed", file=sys.stderr)
        return 1


def _session(args: argparse.Namespace, settings: Settings) -> int:
    """通过会话用例清除历史，缺少删除授权时返回审批退出码。"""
    from .bootstrap import build_session_application

    try:
        result = build_session_application(settings, approve_delete=args.approve_delete).clear(
            args.session_id
        )
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception:
        print(
            "session clear failed; explicit delete approval and valid configuration required",
            file=sys.stderr,
        )
        return 3 if not args.approve_delete else 1


def _parse_decisions(raw_values: Sequence[str]) -> dict[str, Literal["approve", "reject"]]:
    """每个中断 ID 只能出现一次，决定只接受批准或拒绝。"""
    decisions: dict[str, Literal["approve", "reject"]] = {}
    for raw in raw_values:
        key, separator, action = raw.partition("=")
        if not separator or not key or key in decisions:
            raise ValueError("审批 ID 无效或重复")
        if action not in {"approve", "reject"}:
            raise ValueError("审批决定必须为 approve 或 reject")
        decisions[key] = "approve" if action == "approve" else "reject"
    return decisions


async def _control_session(args: argparse.Namespace, settings: Settings) -> dict[str, JsonValue]:
    from .bootstrap import build_agent_application

    async with build_agent_application(
        settings, transport=args.transport, persist_session=True
    ) as application:
        if args.command == "approvals":
            pending = await application.pending_approvals(args.session)
            return {"approvals": {key: value.model_dump() for key, value in pending.items()}}
        if args.command == "confirm-status":
            board = await application.confirm_status(
                args.session,
                task_id=args.task_id,
                revision=args.revision,
                item_ids=tuple(args.item),
            )
            return {"task_status": TypeAdapter(JsonValue).validate_python(board)}
        decisions = _parse_decisions(args.decision)
        snapshots: list[TaskBoard] = []
        result: Run = await application.resume_task(
            args.session, decisions, on_status=snapshots.append
        )
        pending = await application.pending_approvals(args.session)
        return {
            **asdict(result),
            "task_status": TypeAdapter(JsonValue).validate_python(snapshots[-1])
            if snapshots
            else None,
            "approvals": {key: value.model_dump() for key, value in pending.items()},
        }


def _session_control(args: argparse.Namespace, settings: Settings) -> int:
    """执行审批查看、恢复或任务项验收，并返回安全的会话错误。"""

    try:
        payload = asyncio.run(_control_session(args, settings))
        print(json.dumps(payload, ensure_ascii=False, default=str))
        return _run_exit_code(str(payload["status"]) if "status" in payload else None)
    except (ValueError, KeyError):
        print("会话操作无效：请检查会话 ID、当前审批列表或状态版本", file=sys.stderr)
        return 2
    except Exception:
        print("会话操作失败：请检查模型、会话与工具服务配置", file=sys.stderr)
        return 1


def _run(args: argparse.Namespace, settings: Settings) -> int:
    """运行用户任务并输出最终答复、状态栏和待审批操作。"""
    if (
        not args.task.strip()
        or not settings.model
        or not settings.api_key
        or (args.transport == "mcp" and not settings.service_token)
    ):
        print(
            "run requires a task, model, API key and MCP token for MCP transport",
            file=sys.stderr,
        )
        return 2
    from .bootstrap import build_agent_application

    status: TaskBoard | None = None

    def receive_status(value: TaskBoard) -> None:
        nonlocal status
        status = value

    async def execute() -> tuple[Run, dict[str, dict[str, JsonValue]]]:
        async with build_agent_application(
            settings,
            transport=args.transport,
            persist_session=bool(args.session),
            approve_memory_write=args.approve_memory_write,
            approve_memory_delete=args.approve_memory_delete,
        ) as application:
            trace = (
                (lambda node: print(f"langgraph: {node}", file=sys.stderr)) if args.trace else None
            )
            result = await application.run_task(
                args.task,
                session_id=args.session,
                on_node=trace,
                on_status=receive_status,
                constraints=tuple(args.constraint),
                criteria=tuple(args.criterion),
                new_task=args.new_task,
            )
            pending = await application.pending_approvals(result.session_id)
            return result, {key: value.model_dump() for key, value in pending.items()}

    try:
        result, pending = asyncio.run(execute())
    except ContextConfigurationError as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False))
        return 2
    except Exception:
        print(
            json.dumps(
                {"status": "failed", "error": "Agent 初始化失败，请检查模型和 MCP 服务配置"},
                ensure_ascii=False,
            )
        )
        return 1
    print(
        json.dumps(
            {**asdict(result), "task_status": status, "approvals": pending},
            ensure_ascii=False,
            default=str,
        )
    )
    return _run_exit_code(result.status.value)


def _serve_agent(args: argparse.Namespace, settings: Settings) -> int:
    """启动 Agent 常驻服务；显式启动即授权本机工作区内的会话保存。"""
    from .interfaces.agent_http import run_agent_service

    if not settings.workspace.is_dir() or not settings.agent_service_token:
        print("serve-agent requires a workspace and WORKSTATION_AGENT_HTTP_TOKEN", file=sys.stderr)
        return 2
    if not _doctor_agent(settings, require_mcp=args.transport == "mcp"):
        print("Agent service configuration is incomplete", file=sys.stderr)
        return 2
    if args.transport == "mcp" and settings.agent_service_port == settings.service_port:
        print("Agent HTTP and MCP services require distinct ports", file=sys.stderr)
        return 2
    print(f"Agent HTTP service: {settings.agent_service_url}", flush=True)
    try:
        run_agent_service(settings, transport=args.transport)
    except SystemExit as exc:
        if exc.code:
            print("Agent service startup failed; check configuration and tool service", file=sys.stderr)
        return 1 if exc.code else 0
    except Exception:
        print("Agent service startup failed; check model, workspace and tool service", file=sys.stderr)
        return 1
    return 0


def _serve(settings: Settings) -> int:
    """校验本机监听配置，启动 MCP 工具服务并传播退出信号。"""
    from .interfaces.mcp_server import run_local_service

    if not settings.service_token or not settings.workspace.is_dir():
        print(
            "serve requires an existing workspace and WORKSTATION_AGENT_SERVICE_TOKEN",
            file=sys.stderr,
        )
        return 2
    print(f"MCP service: {settings.service_url}", flush=True)
    try:
        run_local_service(build_tool_application(settings), settings)
    except SystemExit as exc:
        # 服务仍然退出；命令行把不同的启动失败码统一为既有的退出码 1。
        if exc.code:
            raise SystemExit(1) from None
        raise
    except (ValueError, OSError):
        print("service startup failed; check configuration and port availability", file=sys.stderr)
        return 1
    return 0
    return 0


def _tools(args: argparse.Namespace, settings: Settings) -> int:
    """显示工具说明，按需输出参数和权限。"""
    application = build_tool_application(settings)
    if args.json:
        print(json.dumps(application.tool_catalog(), ensure_ascii=False))
    else:
        for definition in application.list_tools():
            print(f"{definition.name}: {definition.description}")
    return 0


def _tool(args: argparse.Namespace, settings: Settings) -> int:
    """执行一次受控工具调用，可输出节点进度。"""
    if args.path is not None:
        arguments = {"path": args.path}
    elif args.arguments is None:
        arguments = {}
    else:
        try:
            arguments = json.loads(args.arguments)
        except (json.JSONDecodeError, RecursionError):
            return _print_result(
                ToolResult(
                    tool_name=args.name,
                    content="Tool error: arguments must be valid JSON",
                    success=False,
                    error="arguments must be valid JSON",
                    error_code="invalid_json",
                )
            )
    application = build_tool_application(settings)
    if args.trace:
        for update in application.stream_tool(args.name, arguments):
            print(f"langgraph: {update.node}", file=sys.stderr)
            if update.result is not None:
                result = update.result
    else:
        result = application.execute_tool(args.name, arguments)
    return _print_result(result)


def _init_db(settings: Settings) -> int:
    """通过受控初始化用例执行业务表和官方检查点迁移。"""
    from .bootstrap import build_database_application
    try:
        build_database_application(settings).initialize()
    except Exception:
        print("PostgreSQL initialization failed; check database configuration and permissions", file=sys.stderr)
        return 1
    print("initialized PostgreSQL business and checkpoint schemas")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """加载配置后分派命令，各命令的参数和错误处理由独立函数维护。"""
    args = build_parser().parse_args(argv)
    try:
        settings = Settings.from_env()
    except (ValueError, OSError):
        print("invalid configuration; check WORKSTATION_AGENT_* settings", file=sys.stderr)
        return 2
    match args.command:
        case "doctor":
            return _doctor(args, settings)
        case "rag":
            return _rag(args, settings)
        case "memory":
            return _memory(args, settings)
        case "session":
            return _session(args, settings)
        case "approvals" | "confirm-status" | "resume":
            return _session_control(args, settings)
        case "run":
            return _run(args, settings)
        case "serve":
            return _serve(settings)
        case "serve-agent":
            return _serve_agent(args, settings)
        case "tools":
            return _tools(args, settings)
        case "tool":
            return _tool(args, settings)
        case "init-db":
            return _init_db(settings)
        case _:
            return 2


if __name__ == "__main__":
    raise SystemExit(main())
