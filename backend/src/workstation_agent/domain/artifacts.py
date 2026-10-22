"""定义任务产物的文件信息。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .runs import new_id


@dataclass(frozen=True, slots=True)
class Artifact:
    """记录任务产物的名称、位置和类型。"""
    # 产生该产物的任务标识。
    run_id: str
    # 产物展示名称。
    name: str
    # 产物文件位置；使用前仍需检查工作区限制。
    path: Path
    # 产物的 MIME 类型。
    media_type: str = "application/octet-stream"
    # 产物的唯一标识。
    id: str = field(default_factory=lambda: new_id("artifact"))
    # 记录创建时间，使用 UTC。
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
