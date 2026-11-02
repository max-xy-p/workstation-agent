"""只供集成测试启动的标准 MCP 服务，不访问真实工作台或使用业务凭据。"""

import argparse

from mcp.server import MCPServer
from mcp.types import ToolAnnotations

server = MCPServer("external-test-fixture")


@server.tool(annotations=ToolAnnotations(read_only_hint=True))
def read_item(value: str) -> dict[str, str]:
    """返回文本内容，方便核对外部工具发现与调用。"""
    return {"content": value, "source": "test-fixture"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int)
    args = parser.parse_args()
    if args.port:
        server.run(transport="streamable-http", host="127.0.0.1", port=args.port)
    else:
        server.run()
