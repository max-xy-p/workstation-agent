"""记忆预算和默认关闭的会话、自动提取配置。"""

import os
from dataclasses import dataclass


def _flag(name, default=False):
    raw = os.getenv("WORKSTATION_AGENT_" + name, "true" if default else "false").lower()
    if raw not in {"true", "false", "1", "0"}:
        raise ValueError("记忆布尔配置无效")
    return raw in {"true", "1"}


@dataclass(frozen=True, slots=True)
class MemorySettings:
    """限制本机记忆规模与额外模型调用，启用提取不隐式授权出站。"""

    enabled: bool = True
    sessions: bool = False
    auto_extract: bool = False
    allow_remote: bool = False
    user_id: str = "local"
    project_id: str | None = None
    context_chars: int = 4000
    search_limit: int = 5
    max_records: int = 2000
    extract_chars: int = 6000
    extract_limit: int = 5
    extract_calls: int = 3
    summary_tokens: int = 12000

    def __post_init__(self):
        for name in ("enabled", "sessions", "auto_extract", "allow_remote"):
            if type(getattr(self, name)) is not bool:
                raise ValueError("记忆开关必须为布尔值")
        for name in (
            "context_chars",
            "search_limit",
            "max_records",
            "extract_chars",
            "extract_limit",
            "extract_calls",
            "summary_tokens",
        ):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ValueError("记忆预算必须为正整数")
        if (
            self.search_limit > 20
            or self.extract_limit > 20
            or self.max_records > 10000
            or self.extract_calls > 8
        ):
            raise ValueError("记忆规模超过支持范围")
        if not isinstance(self.user_id, str) or not self.user_id.strip() or len(self.user_id) > 128:
            raise ValueError("记忆用户标识无效")
        if self.project_id is not None and (
            not isinstance(self.project_id, str)
            or not self.project_id.strip()
            or len(self.project_id) > 128
        ):
            raise ValueError("记忆项目标识无效")
        if self.auto_extract and not self.enabled:
            raise ValueError("自动提取需要启用记忆")

    @classmethod
    def from_env(cls):
        """环境变量不接受模糊布尔值，错误不回显输入。"""
        try:
            return cls(
                enabled=_flag("MEMORY_ENABLED", True),
                sessions=_flag("SESSIONS_ENABLED"),
                auto_extract=_flag("MEMORY_AUTO_EXTRACT"),
                allow_remote=_flag("MEMORY_ALLOW_REMOTE"),
                user_id=os.getenv("WORKSTATION_AGENT_USER_ID", "local"),
                project_id=os.getenv("WORKSTATION_AGENT_PROJECT_ID") or None,
                **{
                    name: int(os.getenv("WORKSTATION_AGENT_MEMORY_" + name.upper(), str(default)))
                    for name, default in (
                        ("context_chars", 4000),
                        ("search_limit", 5),
                        ("max_records", 2000),
                        ("extract_chars", 6000),
                        ("extract_limit", 5),
                        ("extract_calls", 3),
                        ("summary_tokens", 12000),
                    )
                },
            )
        except (ValueError, TypeError):
            raise ValueError("记忆配置无效") from None
