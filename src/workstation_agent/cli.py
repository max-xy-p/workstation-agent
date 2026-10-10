"""提供任务执行、工具调用和配置检查的命令行入口。"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from dataclasses import asdict
from importlib.metadata import version

from pydantic import TypeAdapter
from pydantic import __version__ as pydantic_version

from . import __version__
from .bootstrap import build_infrastructure, build_tool_application
from .config import Settings
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
    doctor.add_argument("--agent", action="store_true", help="check model and MCP configuration")
    run = subparsers.add_parser("run", help="run a task with the official LangChain agent")
    run.add_argument("task")
    run.add_argument("--transport", choices=("mcp", "local"), default="mcp")
    run.add_argument("--trace", action="store_true", help="print official graph node names")
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
        if args.agent:
            ready = ready and bool(settings.model and settings.api_key and settings.service_token)
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

        async def execute():
            async with build_agent_application(settings, transport=args.transport) as application:
                trace = (
                    (lambda node: print(f"langgraph: {node}", file=sys.stderr))
                    if args.trace
                    else None
                )
                return await application.run_task(args.task, on_node=trace)

        try:
            result = asyncio.run(execute())
        except Exception:
            print(
                json.dumps(
                    {"status": "failed", "error": "Agent 初始化失败，请检查模型和 MCP 服务配置"},
                    ensure_ascii=False,
                )
            )
            return 1
        print(json.dumps(asdict(result), ensure_ascii=False, default=str))
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
