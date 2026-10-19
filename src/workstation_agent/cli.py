"""提供任务执行、工具调用和配置检查的命令行入口。"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from dataclasses import asdict
from importlib.metadata import version

from pydantic import JsonValue, TypeAdapter
from pydantic import __version__ as pydantic_version

from . import __version__
from .bootstrap import build_infrastructure, build_tool_application
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
    subparsers.add_parser("init-db", help="initialize the local SQLite state database")
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


def main(argv: Sequence[str] | None = None) -> int:
    """根据命令调用应用入口并返回退出码。"""
    args = build_parser().parse_args(argv)
    try:
        settings = Settings.from_env()
    except (ValueError, OSError):
        print("invalid configuration; check WORKSTATION_AGENT_* settings", file=sys.stderr)
        return 2

    if args.command == "doctor":
        print(f"workspace: {settings.workspace}")
        print(f"workspace exists: {settings.workspace.is_dir()}")
        print(f"model: {settings.model or '(not configured)'}")
        print(f"max steps: {settings.max_steps}")
        print(f"database: {settings.database_path}")
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
        ready = settings.workspace.is_dir() and (not args.service or bool(settings.service_token))
        print(f"model base URL: {settings.model_base_url}")
        print(f"model API key: {'configured' if settings.api_key else '(not configured)'}")
        print(f"model timeout: {settings.model_timeout}")
        print(f"run timeout (including approval wait): {settings.run_timeout}")
        print(f"Context task board token cap: {settings.context.board_tokens}")
        print(f"max tool calls: {settings.max_tool_calls}")
        print(f"RAG mode: {'hybrid' if settings.rag.embedding_model else 'bm25'}")
        print(f"RAG contextual model: {settings.rag.context_model or '(structural prefix only)'}")
        print(f"RAG remote authorized: {settings.rag.allow_remote}")
        print(f"RAG chunk size / overlap: {settings.rag.chunk_size} / {settings.rag.chunk_overlap}")
        print(f"RAG context budget (UTF-8 upper bound): {settings.rag.context_budget}")
        try:
            print(f"RAG database: {settings.rag_database_path}")
        except ValueError:
            print("RAG data path is outside workspace")
            ready = False
        if args.rag and (settings.rag.embedding_model or settings.rag.context_model):
            ready = ready and bool(settings.api_key and settings.rag.allow_remote)
        if args.rag:
            try:
                health = build_tool_application(settings).execute_tool("rag_status", {})
            except (ValueError, OSError):
                print("RAG index check failed")
                return 1
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
        print(f"langgraph-checkpoint-sqlite: {version('langgraph-checkpoint-sqlite')}")
        print(f"langmem: {version('langmem')}")
        try:
            print(f"Memory database: {settings.memory_database_path}")
            print(f"Session database: {settings.session_database_path}")
            if args.memory and settings.memory.enabled:
                from .bootstrap import build_memory_application

                report = build_memory_application(settings).execute("list_memories", {"limit": 1})
                print(f"Memory records in allowed scopes: {report['total']}")
        except Exception:
            print("Memory storage check failed")
            ready = False
        if settings.memory.auto_extract:
            ready = ready and bool(
                settings.memory.allow_remote and settings.model and settings.api_key
            )
        if args.agent:
            ready = ready and bool(settings.model and settings.api_key and settings.service_token)
            try:
                from .context.budget import resolve_settings
                from .models.providers.openai import build_chat_model

                budget = resolve_settings(
                    settings.context, build_chat_model(settings)
                ).input_budget()
                print(f"Context input budget: {budget}")
            except ValueError:
                print(
                    "Context budget unavailable: configure CONTEXT_WINDOW and check output limits"
                )
                ready = False
        return 0 if ready else 1

    if args.command == "rag":
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

    if args.command == "memory":
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

    if args.command == "session":
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

    if args.command in {"approvals", "resume", "confirm-status"}:
        from typing import Literal

        from .bootstrap import build_agent_application

        async def control_session() -> dict[str, JsonValue]:
            async with build_agent_application(
                settings, transport=args.transport, persist_session=True
            ) as application:
                if args.command == "approvals":
                    pending = await application.pending_approvals(args.session)
                    return {
                        "approvals": {key: value.model_dump() for key, value in pending.items()}
                    }
                if args.command == "confirm-status":
                    board = await application.confirm_status(
                        args.session,
                        task_id=args.task_id,
                        revision=args.revision,
                        item_ids=tuple(args.item),
                    )
                    return {"task_status": TypeAdapter(JsonValue).validate_python(board)}
                decisions: dict[str, Literal["approve", "reject"]] = {}
                for raw in args.decision:
                    key, separator, action = raw.partition("=")
                    if not separator or not key or key in decisions:
                        raise ValueError("审批 ID 无效或重复")
                    if action not in {"approve", "reject"}:
                        raise ValueError("审批决定必须为 approve 或 reject")
                    decisions[key] = "approve" if action == "approve" else "reject"
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

        try:
            payload = asyncio.run(control_session())
            print(json.dumps(payload, ensure_ascii=False, default=str))
            return (
                3
                if payload.get("status") == "waiting_approval"
                else (1 if payload.get("status") in {"failed", "cancelled", "limit_reached"} else 0)
            )
        except (ValueError, KeyError):
            print("会话操作无效：请检查会话 ID、当前审批列表或状态版本", file=sys.stderr)
            return 2
        except Exception:
            print("会话操作失败：请检查模型、会话与工具服务配置", file=sys.stderr)
            return 1

    if args.command == "run":
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
                    (lambda node: print(f"langgraph: {node}", file=sys.stderr))
                    if args.trace
                    else None
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
        return (
            0
            if result.status.value == "completed"
            else 3
            if result.status.value == "waiting_approval"
            else 1
        )

    if args.command == "serve":
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
        except (ValueError, OSError):
            print(
                "service startup failed; check configuration and port availability", file=sys.stderr
            )
            return 1
        return 0

    if args.command == "tools":
        application = build_tool_application(settings)
        if args.json:
            print(json.dumps(application.tool_catalog(), ensure_ascii=False))
        else:
            for definition in application.list_tools():
                print(f"{definition.name}: {definition.description}")
        return 0

    if args.command == "tool":
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

    if args.command == "init-db":
        infrastructure = build_infrastructure(settings)
        infrastructure.runs.initialize()
        print(f"initialized database: {settings.database_path}")
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
