"""从环境变量加载运行配置。"""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from .context.config import ContextSettings
from .memory.config import MemorySettings
from .rag.config import RagSettings


@dataclass(frozen=True, slots=True)
class Settings:
    """保存 Agent 和本机工具服务的运行配置。"""

    # 文件工具允许访问的根目录。
    workspace: Path
    # 单次任务的模型调用次数上限。
    max_steps: int = 8
    # 使用的模型名称，运行模型任务前需要配置。
    model: str | None = None
    # 保存任务和步骤记录的 agent.db 存放目录；默认入口不启用保存。
    data_dir: Path | None = None
    # 单次工具输出的字符上限，包含截断标记。
    tool_output_chars: int = 50_000

    # 模型接口密钥，打印配置对象时隐藏。
    api_key: str | None = field(default=None, repr=False)
    # OpenAI 兼容模型接口的基础地址。
    model_base_url: str = "https://api.openai.com/v1"
    # 模型请求和 MCP 读取的超时秒数。
    model_timeout: float = 30.0
    # 整次运行的截止秒数，包含审批等待，恢复不能重新计时。
    run_timeout: float = 300.0
    # 单次任务的工具调用次数上限。
    max_tool_calls: int = 32
    # 本机 MCP 工具服务的监听端口。
    service_port: int = 8765
    # 本机工具服务的认证令牌，打印配置对象时隐藏。
    service_token: str | None = field(default=None, repr=False)

    # RAG 模型与资源预算，默认不向外发送文档。
    rag: RagSettings = field(default_factory=RagSettings)

    memory: MemorySettings = field(default_factory=MemorySettings)
    context: ContextSettings = field(default_factory=ContextSettings)

    @property
    def memory_database_path(self) -> Path:
        """记忆与会话数据库必须位于工作区内。"""
        return self._memory_path("memory.db")

    @property
    def session_database_path(self) -> Path:
        """框架会话与长期记忆分库保存，独立管理迁移。"""
        return self._memory_path("sessions.db")

    def _memory_path(self, name):
        path = self.database_path.with_name(name).resolve()
        if not path.is_relative_to(self.workspace.resolve()):
            raise ValueError("记忆数据目录必须位于工作区内")
        return path

    @property
    def memory_access(self):
        """身份来自配置而非模型参数，工作区标识使用规范化路径。"""
        import hashlib
        import os

        from .memory.models import MemoryAccess

        identity = os.path.normcase(str(self.workspace.resolve()))
        return MemoryAccess(
            user_id=self.memory.user_id,
            workspace_id=hashlib.sha256(identity.encode()).hexdigest(),
            project_id=self.memory.project_id,
        )

    @property
    def rag_database_path(self) -> Path:
        """RAG 索引路径必须解析到工作区内。"""
        path = self.database_path.with_name("rag.db").resolve()
        if not path.is_relative_to(self.workspace.resolve()):
            raise ValueError("RAG 数据目录必须位于工作区内")
        return path

    def __post_init__(self) -> None:
        endpoint = urlparse(self.model_base_url)
        if (
            endpoint.scheme not in {"http", "https"}
            or not endpoint.hostname
            or endpoint.username
            or endpoint.password
            or endpoint.query
            or endpoint.fragment
        ):
            raise ValueError("模型 base_url 必须为不含凭据、查询和片段的 HTTP 地址")
        if not math.isfinite(self.model_timeout) or self.model_timeout <= 0:
            raise ValueError("模型超时必须为正数")
        if not math.isfinite(self.run_timeout) or self.run_timeout <= 0:
            raise ValueError("整次任务超时必须为正数")
        if type(self.max_tool_calls) is not int or self.max_tool_calls < 1:
            raise ValueError("工具调用上限必须为正整数")
        if type(self.service_port) is not int or not 1 <= self.service_port <= 65535:
            raise ValueError("WORKSTATION_AGENT_SERVICE_PORT must be between 1 and 65535")
        if self.service_token is not None and not re.fullmatch(
            r"[A-Za-z0-9_-]{32,256}", self.service_token
        ):
            raise ValueError("WORKSTATION_AGENT_SERVICE_TOKEN must be 32-256 URL-safe characters")

    @property
    def service_url(self) -> str:
        """返回本机 MCP 工具服务地址。"""
        return f"http://127.0.0.1:{self.service_port}/mcp"

    @property
    def database_path(self) -> Path:
        """返回 agent.db 的完整路径。"""
        return (self.data_dir or self.workspace / ".workstation-agent") / "agent.db"

    @classmethod
    def from_env(cls) -> Settings:
        """从环境变量读取并校验配置。"""
        workspace = Path(os.getenv("WORKSTATION_AGENT_WORKSPACE", ".")).resolve()
        raw_steps = os.getenv("WORKSTATION_AGENT_MAX_STEPS", "8")
        try:
            max_steps = int(raw_steps)
        except ValueError as exc:
            raise ValueError("WORKSTATION_AGENT_MAX_STEPS must be an integer") from exc
        if max_steps < 1:
            raise ValueError("WORKSTATION_AGENT_MAX_STEPS must be at least 1")

        model = os.getenv("WORKSTATION_AGENT_MODEL") or None
        raw_data_dir = os.getenv("WORKSTATION_AGENT_DATA_DIR")
        data_dir = Path(raw_data_dir).resolve() if raw_data_dir else None
        try:
            tool_output_chars = int(os.getenv("WORKSTATION_AGENT_TOOL_OUTPUT_CHARS", "50000"))
        except ValueError as exc:
            raise ValueError("WORKSTATION_AGENT_TOOL_OUTPUT_CHARS must be an integer") from exc
        if tool_output_chars < 1:
            raise ValueError("WORKSTATION_AGENT_TOOL_OUTPUT_CHARS must be at least 1")
        try:
            service_port = int(os.getenv("WORKSTATION_AGENT_SERVICE_PORT", "8765"))
        except ValueError as exc:
            raise ValueError("WORKSTATION_AGENT_SERVICE_PORT must be an integer") from exc
        try:
            model_timeout = float(os.getenv("WORKSTATION_AGENT_MODEL_TIMEOUT", "30"))
            run_timeout = float(os.getenv("WORKSTATION_AGENT_RUN_TIMEOUT", "300"))
            max_tool_calls = int(os.getenv("WORKSTATION_AGENT_MAX_TOOL_CALLS", "32"))
        except ValueError:
            raise ValueError("模型超时或工具调用上限配置无效") from None
        return cls(
            workspace=workspace,
            max_steps=max_steps,
            model=model,
            data_dir=data_dir,
            tool_output_chars=tool_output_chars,
            api_key=os.getenv("WORKSTATION_AGENT_API_KEY") or os.getenv("OPENAI_API_KEY") or None,
            model_base_url=os.getenv("WORKSTATION_AGENT_BASE_URL", "https://api.openai.com/v1"),
            model_timeout=model_timeout,
            run_timeout=run_timeout,
            max_tool_calls=max_tool_calls,
            service_port=service_port,
            service_token=os.getenv("WORKSTATION_AGENT_SERVICE_TOKEN") or None,
            rag=RagSettings.from_env(),
            memory=MemorySettings.from_env(),
            context=ContextSettings.from_env(),
        )
