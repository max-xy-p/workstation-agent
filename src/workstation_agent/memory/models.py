"""定义记忆记录及其查询条件。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

from ..domain.runs import new_id


class MemoryKind(StrEnum):
    """定义记忆类别，供检索过滤。"""
    CONVERSATION = "conversation"
    SEMANTIC = "semantic"
    EPISODIC = "episodic"
    PROCEDURAL = "procedural"


class MemoryScope(StrEnum):
    """定义记忆所属范围的类别。"""
    USER = "user"
    WORKSPACE = "workspace"
    PROJECT = "project"
    SESSION = "session"


@dataclass(frozen=True, slots=True)
class MemoryRecord:
    """保存用户或任务经验及其来源和过期时间。"""
    # 可复用的用户或任务经验。
    content: str
    # 供检索过滤的记忆类别。
    kind: MemoryKind
    # 记忆所属范围的类别。
    scope: MemoryScope
    # 所属用户、项目等对象的标识。
    scope_id: str
    # 记忆的唯一标识。
    id: str = field(default_factory=lambda: new_id("memory"))
    # 重要性排序权重。
    importance: float = 0.5
    # 可信度排序权重。
    confidence: float = 1.0
    # 来源任务标识，可为空。
    source_run_id: str | None = None
    # 创建时间，使用 UTC。
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    # 过期时间，None 表示不过期。
    expires_at: datetime | None = None
    # 附加字符串属性。
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class MemoryQuery:
    """定义记忆检索文本、过滤条件和数量上限。"""
    # 记忆检索文本。
    text: str
    # 范围类别过滤条件，None 表示不限。
    scope: MemoryScope | None = None
    # 所属对象过滤条件，None 表示不限。
    scope_id: str | None = None
    # 允许的记忆类别，空元组表示不限。
    kinds: tuple[MemoryKind, ...] = ()
    # 最多返回的记忆条数。
    limit: int = 5
