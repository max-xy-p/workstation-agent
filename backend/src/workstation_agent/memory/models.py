"""定义长期文本记忆、可信访问范围和提取候选。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..domain.runs import new_id

Identifier = str


class MemoryScope(BaseModel):
    """绑定所有者及工作区/项目，命名空间不接受模型自由指定。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    user_id: str = Field(min_length=1, max_length=128)
    level: Literal["user", "workspace", "project"] = "workspace"
    workspace_id: str | None = None
    project_id: str | None = None

    @model_validator(mode="after")
    def validate_scope(self):
        if self.level != "user" and not self.workspace_id:
            raise ValueError("工作区范围需要工作区标识")
        if self.level == "project" and not self.project_id:
            raise ValueError("项目范围需要项目标识")
        if self.level == "user" and (self.workspace_id or self.project_id):
            raise ValueError("用户范围不得附带项目或工作区")
        if self.level == "workspace" and self.project_id:
            raise ValueError("工作区范围不得附带项目")
        return self

    @property
    def namespace(self) -> str:
        """用无歧义编码构造存储分区。"""
        return json.dumps(
            [self.user_id, self.level, self.workspace_id, self.project_id],
            ensure_ascii=False,
            separators=(",", ":"),
        )


class MemoryAccess(BaseModel):
    """由应用配置构造可信身份，供每次查询与修改检查。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    user_id: str = Field(min_length=1, max_length=128)
    workspace_id: str = Field(min_length=1, max_length=128)
    project_id: str | None = Field(default=None, max_length=128)

    def scope(self, level: str | None = None) -> MemoryScope:
        """默认使用当前最窄范围，禁止缺少项目时退回所有项目。"""
        level = level or ("project" if self.project_id else "workspace")
        return MemoryScope(
            user_id=self.user_id,
            level=level,
            workspace_id=self.workspace_id if level != "user" else None,
            project_id=self.project_id if level == "project" else None,
        )

    @property
    def scopes(self) -> tuple[MemoryScope, ...]:
        """返回允许召回的范围，项目只包含当前项目。"""
        levels = ("user", "workspace", "project") if self.project_id else ("user", "workspace")
        return tuple(self.scope(level) for level in levels)

    def allows(self, scope: MemoryScope) -> bool:
        """按完整命名空间检查所有权，不依赖记录 ID 的保密性。"""
        return scope in self.scopes


class MemorySource(BaseModel):
    """保存来源标识，不复制原始对话和工具输出。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    origin: Literal["explicit", "extracted"]
    session_id: str | None = None
    message_id: str | None = None
    run_id: str | None = None

    @model_validator(mode="after")
    def require_reference(self):
        if not any((self.session_id, self.message_id, self.run_id)):
            raise ValueError("记忆需要来源引用")
        return self


class MemoryRecord(BaseModel):
    """保存一条可独立理解的文本及其作用域、来源和版本。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    id: str = Field(default_factory=lambda: new_id("memory"))
    content: str = Field(min_length=1, max_length=4000, repr=False)
    kind: Literal["preference", "decision", "experience"] = "preference"
    scope: MemoryScope
    sources: tuple[MemorySource, ...] = Field(min_length=1)
    version: int = Field(default=1, ge=1)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    expires_at: datetime | None = None

    @field_validator("content")
    @classmethod
    def nonblank(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("记忆正文不能为空")
        return value

    @field_validator("created_at", "updated_at", "expires_at")
    @classmethod
    def timezone_required(cls, value):
        if value is not None and value.utcoffset() is None:
            raise ValueError("记忆时间必须带时区")
        return value


class MemoryCandidate(BaseModel):
    """框架仅能提出有限的文本变更，来源与范围由应用补充。"""

    model_config = ConfigDict(extra="forbid")
    content: str = Field(min_length=1, max_length=4000, repr=False)
    kind: Literal["preference", "decision", "experience"] = "preference"
    target_id: str | None = None
    expected_version: int | None = Field(default=None, ge=1)
    evidence: str = Field(min_length=1, max_length=4000, repr=False)
    certain: bool = False
