"""定义可信调用方配置的 MCP 连接和工具权限，不从远端注解推导授权。"""

from __future__ import annotations

import json
import os
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator

from ..safety.permissions import PermissionLevel

type ToolPermission = Literal["read", "write", "sensitive", "delete"]

PERMISSIONS: dict[ToolPermission, PermissionLevel] = {
    "read": PermissionLevel.SAFE_READ,
    "write": PermissionLevel.CONTROLLED_WRITE,
    "sensitive": PermissionLevel.SENSITIVE,
    "delete": PermissionLevel.DESTRUCTIVE,
}


class MCPServerConfig(BaseModel):
    """只暴露显式列出的工具；凭据通过环境变量名引用，stdio 启动参数仅由配置提供。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,15}$")
    transport: Literal["streamable-http", "stdio"] = "streamable-http"
    url: str | None = None
    token_env: str | None = Field(default=None, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    command: str | None = None
    args: list[str] = Field(default_factory=list, repr=False, max_length=128)
    env: list[str] = Field(default_factory=list, max_length=64)
    secret_env: list[str] = Field(default_factory=list, max_length=64)
    cwd: str = "."
    tools: dict[str, ToolPermission] = Field(min_length=1, max_length=256)

    @field_validator("tools")
    @classmethod
    def validate_tools(cls, value: dict[str, ToolPermission]) -> dict[str, ToolPermission]:
        """限制配置中的远程名称，模型可见的名称由接入层另行生成。"""
        if any(not name or len(name) > 128 for name in value):
            raise ValueError("MCP 工具名称无效")
        return value

    @model_validator(mode="after")
    def validate_transport(self) -> MCPServerConfig:
        """拒绝含凭据的 URL 和不匹配的传输配置，不回显配置正文。"""
        if self.transport == "streamable-http":
            parsed = urlparse(self.url or "")
            if (not parsed.hostname or parsed.scheme not in {"http", "https"}
                    or parsed.username or parsed.password or parsed.query or parsed.fragment
                    or (parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"})
                    or self.command is not None or self.args or self.env or self.secret_env or self.cwd != "."):
                raise ValueError("MCP HTTP 连接配置无效；非本机地址必须使用 HTTPS")
        elif not self.command or self.url is not None or self.token_env is not None:
            raise ValueError("MCP stdio 连接配置无效")
        return self

    def credential(self) -> str | None:
        """在建立连接时读取凭据，不把实际值保存在配置对象中。"""
        value = os.getenv(self.token_env) if self.token_env else None
        if self.token_env and (not value or "\r" in value or "\n" in value):
            raise ValueError("MCP 连接需要有效的凭据环境变量")
        return value


def external_servers_from_env() -> tuple[MCPServerConfig, ...]:
    """从 JSON 数组读取外部服务配置；未配置时不连接任何外部服务。"""
    try:
        data = json.loads(os.getenv("WORKSTATION_AGENT_MCP_SERVERS", "[]"))
        servers = TypeAdapter(list[MCPServerConfig]).validate_python(data)
        names = [server.name for server in servers]
        if len(names) != len(set(names)) or len(names) > 16:
            raise ValueError
        return tuple(servers)
    except (ValueError, TypeError):
        raise ValueError("WORKSTATION_AGENT_MCP_SERVERS 必须为有效且名称不重复的 MCP 配置数组") from None
