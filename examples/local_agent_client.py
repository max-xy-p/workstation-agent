"""演示独立进程调用本机 MCP 工具。"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from workstation_agent.config import Settings


async def call(settings: Settings, name: str, arguments: dict) -> int:
    """连接服务并输出工具列表和调用结果。"""
    async with httpx2.AsyncClient(
        headers={"Authorization": f"Bearer {settings.service_token}"},
        trust_env=False, timeout=15,
    ) as http:
        async with Client(streamable_http_client(settings.service_url, http_client=http)) as client:
            catalog = await client.list_tools()
            print(json.dumps({"tools": [tool.name for tool in catalog.tools]}, ensure_ascii=False))
            result = await client.call_tool(name, arguments)
            print(json.dumps(result.structured_content, ensure_ascii=False))
            return 1 if result.is_error else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tool", nargs="?", default="list_files")
    parser.add_argument("--path", help="workspace-relative path")
    args = parser.parse_args()
    try:
        settings = Settings.from_env()
        if not settings.service_token:
            raise ValueError("missing token")
    except (ValueError, OSError):
        print("client requires valid WORKSTATION_AGENT_SERVICE_PORT and SERVICE_TOKEN", file=sys.stderr)
        return 2
    try:
        return asyncio.run(call(settings, args.tool, {"path": args.path} if args.path else {}))
    except Exception:
        # 原始异常可能包含凭据，不直接输出。
        print("cannot call local service; check endpoint, token, and service status", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
