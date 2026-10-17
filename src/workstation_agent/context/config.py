"""定义完整模型请求的预算，未知模型的容量必须由部署者提供。"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass


class ContextConfigurationError(ValueError):
    """表示可以直接向使用者说明的上下文配置错误，不包含配置原始值。"""


@dataclass(frozen=True, slots=True)
class ContextSettings:
    """限制输入、输出和额外摘要；这些限制不依赖会话是否保存。"""

    window: int | None = None
    input_limit: int | None = None
    output_limit: int | None = None
    output_tokens: int = 2048
    safety_tokens: int = 1024
    trigger_ratio: float = 0.8
    summarize: bool = False
    refresh_limit: int = 2

    def __post_init__(self) -> None:
        for value in (self.window, self.input_limit, self.output_limit):
            if value is not None and (type(value) is not int or value < 1):
                raise ContextConfigurationError("模型容量必须为正整数")
        for value in (self.output_tokens, self.safety_tokens, self.refresh_limit):
            if type(value) is not int or value < 0:
                raise ContextConfigurationError("上下文预算必须为非负整数")
        if not self.output_tokens or not math.isfinite(self.trigger_ratio):
            raise ContextConfigurationError("输出预留和压缩阈值无效")
        if not 0 < self.trigger_ratio <= 1 or type(self.summarize) is not bool:
            raise ContextConfigurationError("压缩配置无效")
        if self.output_limit is not None and self.output_tokens > self.output_limit:
            raise ContextConfigurationError("输出预留超过模型输出上限")
        if self.window is not None:
            self.input_budget()

    def input_budget(self) -> int:
        """计算可发送的输入量；缺少窗口配置时拒绝猜测模型容量。"""
        if self.window is None:
            raise ContextConfigurationError(
                "请配置 WORKSTATION_AGENT_CONTEXT_WINDOW，或使用有可信容量资料的模型"
            )
        budget = min(self.input_limit or self.window, self.window - self.output_tokens)
        budget -= self.safety_tokens
        if budget < 1:
            raise ContextConfigurationError("模型窗口无法容纳输出预留与安全余量")
        return budget

    @classmethod
    def from_env(cls) -> ContextSettings:
        """读取上下文预算环境变量；无模型的普通诊断允许暂不配置容量。"""
        prefix = "WORKSTATION_AGENT_CONTEXT_"
        raw_summary = os.getenv(prefix + "SUMMARIZE", "false").lower()
        if raw_summary not in {"true", "false"}:
            raise ContextConfigurationError("CONTEXT_SUMMARIZE 必须为 true 或 false")
        return cls(
            window=int(os.environ[prefix + "WINDOW"]) if os.getenv(prefix + "WINDOW") else None,
            input_limit=int(os.environ[prefix + "INPUT_LIMIT"])
            if os.getenv(prefix + "INPUT_LIMIT")
            else None,
            output_limit=int(os.environ[prefix + "OUTPUT_LIMIT"])
            if os.getenv(prefix + "OUTPUT_LIMIT")
            else None,
            output_tokens=int(os.getenv(prefix + "OUTPUT_TOKENS", "2048")),
            safety_tokens=int(os.getenv(prefix + "SAFETY_TOKENS", "1024")),
            trigger_ratio=float(os.getenv(prefix + "TRIGGER_RATIO", "0.8")),
            summarize=raw_summary == "true",
            refresh_limit=int(os.getenv(prefix + "REFRESH_LIMIT", "2")),
        )
