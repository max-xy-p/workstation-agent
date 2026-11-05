"""处理文本记忆的确定性规则，模型仅提供变更建议。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from ..retrieval.strategy import RetrievalStrategy, validate_hits
from ..tools.contracts import ToolError
from .models import MemoryAccess, MemoryRecord, MemoryScope, MemorySource
from .repository import MemoryRepository
from .retrieval import MemoryRetrievalStrategy

_SECRET = re.compile(
    r"(?i)(?:\b(?:api[_-]?key|access[_-]?token|password|secret|authorization)"
    r"[\s\"']*[:=]\s*\S+|\bbearer\s+\S+|\bsk-[A-Za-z0-9_-]{16,}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----)"
)


def validate_content(text: str, secrets: tuple[str | None, ...] = ()) -> str:
    """拒绝明显秘密和已知凭据，错误中不携带原文。"""
    if not text.strip() or len(text) > 4000:
        raise ToolError("记忆正文为空或超过长度限制", code="invalid_arguments")
    if _SECRET.search(text) or any(secret and secret in text for secret in secrets):
        raise ToolError("内容含敏感凭据，拒绝保存或提取", code="sensitive_content")
    return text.strip()


def redact(text: str, secrets: tuple[str | None, ...] = ()) -> str:
    """为会话持久化移除已知凭据及常见秘密模式。"""
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[已脱敏]")
    return _SECRET.sub("[已脱敏]", text)


@dataclass(frozen=True, slots=True)
class MemoryChange:
    """已整理的单条新增或修改，更新目标须带读取时的版本。"""

    content: str
    kind: Literal["preference", "decision", "experience"]
    target_id: str | None
    expected_version: int | None
    expiry: datetime | Literal["keep"] | None


@dataclass(frozen=True, slots=True)
class MemoryWritePlan:
    """保存待提交的记录及准备时观察到的版本，不执行写入。"""

    records: tuple[MemoryRecord, ...]
    expected: dict[str, int | None]
    generation: int


class MemoryService:
    """通过仓库协议校验范围、内容和版本，不直接访问系统资源。"""

    def __init__(
        self,
        repository: MemoryRepository,
        *,
        retrieval: RetrievalStrategy[MemoryRecord] | None = None,
        max_records: int = 2000,
        secrets: tuple[str | None, ...] = (),
    ) -> None:
        self.repository: MemoryRepository = repository
        self.retrieval: RetrievalStrategy[MemoryRecord] = (
            retrieval if retrieval is not None else MemoryRetrievalStrategy()
        )
        self.max_records = max_records
        self.secrets = secrets

    def records(
        self, access: MemoryAccess, *, include_expired: bool = False
    ) -> tuple[MemoryRecord, ...]:
        """仅在可信范围内列出记录，默认排除过期内容。"""
        now = datetime.now(UTC)
        return tuple(
            record
            for record in self.repository.records(access.scopes)
            if access.allows(record.scope)
            and (include_expired or record.expires_at is None or record.expires_at > now)
        )

    def get(
        self, access: MemoryAccess, memory_id: str, *, include_expired: bool = True
    ) -> MemoryRecord:
        """按 ID 读取也检查完整范围，不暴露其他所有者是否存在。"""
        record = next(
            (r for r in self.records(access, include_expired=include_expired) if r.id == memory_id),
            None,
        )
        if record is None:
            raise ToolError("记忆不存在或不在允许范围", code="not_found")
        return record

    def search(
        self, access: MemoryAccess, text: str, *, limit: int = 5
    ) -> tuple[MemoryRecord, ...]:
        """先过滤范围和有效期，再调用策略；返回正文始终来自已过滤的记录。"""
        if limit < 1:
            raise ToolError("检索数量必须大于零", code="invalid_arguments")
        records = self.records(access)
        if not records:
            return ()
        by_id = {r.id: r for r in records}
        hits = validate_hits(
            self.retrieval.retrieve(text, records, limit=limit), set(by_id), limit
        )
        return tuple(by_id[hit.id] for hit in hits)

    def remember(
        self,
        access: MemoryAccess,
        *,
        content: str,
        kind: Literal["preference", "decision", "experience"],
        scope: MemoryScope,
        source: MemorySource,
        expires_at: datetime | None = None,
    ) -> MemoryRecord:
        """合并完全相同的有效笔记来源，不把语义相似当作相同事实。"""
        old = next(
            (
                r
                for r in self.records(access)
                if r.scope == scope and r.content == content.strip() and r.kind == kind
            ),
            None,
        )
        if old:
            record = old.model_copy(
                update={
                    "sources": tuple(dict.fromkeys((*old.sources, source))),
                    "version": old.version + 1,
                    "updated_at": datetime.now(UTC),
                    "expires_at": expires_at if expires_at is not None else old.expires_at,
                }
            )
        else:
            record = MemoryRecord(
                content=content, kind=kind, scope=scope, sources=(source,), expires_at=expires_at
            )
        self.save(access, record, expected_version=old.version if old else None)
        return record

    def update(
        self,
        access: MemoryAccess,
        memory_id: str,
        version: int,
        *,
        content: str,
        source: MemorySource,
        expiry: datetime | Literal["keep"] | None = "keep",
    ) -> MemoryRecord:
        """更新明确指定的记录，省略有效期时保留原值。"""
        old = self.get(access, memory_id)
        record = MemoryRecord.model_validate(
            {
                **old.model_dump(),
                "content": content,
                "version": version + 1,
                "updated_at": datetime.now(UTC),
                "sources": tuple(dict.fromkeys((*old.sources, source))),
                "expires_at": old.expires_at if expiry == "keep" else expiry,
            }
        )
        self.save(access, record, expected_version=version)
        return record

    def prepare_batch(
        self, access: MemoryAccess, scope: MemoryScope, changes: tuple[MemoryChange, ...],
        source: MemorySource,
    ) -> MemoryWritePlan:
        """整理明确候选的新增和更新，保留审批及提交需要检查的版本。"""
        if not access.allows(scope):
            raise ToolError("记忆范围不允许", code="scope_denied")
        generation = self.repository.generation(scope)
        current = self.records(access, include_expired=True)
        owned = {record.id: record for record in current if record.scope == scope}
        now = datetime.now(UTC)
        exact = {(record.kind, record.content): record for record in owned.values()
                 if record.expires_at is None or record.expires_at > now}
        records: list[MemoryRecord] = []
        expected: dict[str, int | None] = {}
        seen: set[tuple[str, str]] = set()
        for change in changes:
            content = validate_content(change.content, self.secrets)
            old = owned.get(change.target_id) if change.target_id else exact.get((change.kind, content))
            if change.target_id and (old is None or old.version != change.expected_version):
                raise ToolError("记忆目标或版本已变化，请重新读取", code="version_conflict")
            kind = old.kind if old else change.kind
            key = (kind, content)
            duplicate = exact.get(key)
            if key in seen or (old and old.id in expected) or (old and duplicate and old.id != duplicate.id):
                raise ToolError("候选重复或与已有记忆冲突，请明确保留哪一条", code="memory_conflict")
            seen.add(key)
            expiry = (old.expires_at if old else None) if change.expiry == "keep" else change.expiry
            if old:
                record = old.model_copy(update={
                    "content": content, "expires_at": expiry, "version": old.version + 1,
                    "updated_at": now, "sources": tuple(dict.fromkeys((*old.sources, source))),
                })
            else:
                record = MemoryRecord(content=content, kind=kind, scope=scope, sources=(source,), expires_at=expiry)
            records.append(record)
            expected[record.id] = old.version if old else None
        return MemoryWritePlan(tuple(records), expected, generation)

    def save(
        self,
        access: MemoryAccess,
        record: MemoryRecord,
        *,
        expected_version: int | None = None,
        generation: int | None = None,
        batch_id: str | None = None,
    ) -> MemoryRecord:
        """新增或更新独立笔记，显式修改必须指定已读取版本。"""
        self.commit(
            access,
            (record,),
            {record.id: expected_version},
            record.scope,
            generation=generation,
            batch_id=batch_id,
        )
        return record

    def commit(
        self,
        access: MemoryAccess,
        records: tuple[MemoryRecord, ...],
        expected: dict[str, int | None],
        scope: MemoryScope,
        *,
        generation: int | None = None,
        batch_id: str | None = None,
    ) -> bool:
        """先校验整个候选批次，再由仓库以事务一次提交。"""
        if not access.allows(scope):
            raise ToolError("记忆范围不允许", code="scope_denied")
        current = {r.id: r for r in self.repository.records((scope,))}
        for record in records:
            if record.scope != scope:
                raise ToolError("记忆范围不一致", code="scope_denied")
            validate_content(record.content, self.secrets)
            old = current.get(record.id)
            version = expected.get(record.id)
            if (old is None and version is not None) or (
                old is not None and old.version != version
            ):
                raise ToolError("记忆版本已改变，请重新读取", code="version_conflict")
            if record.version != (version or 0) + 1:
                raise ToolError("记忆版本无效", code="version_conflict")
        new_ids = {r.id for r in records} - current.keys()
        if len(current) + len(new_ids) > self.max_records:
            raise ToolError("记忆条数超过范围预算", code="resource_limit")
        return self.repository.apply(
            records, expected, scope, generation=generation, batch_id=batch_id
        )

    def forget(self, access: MemoryAccess, memory_id: str, version: int) -> bool:
        """删除正文并提升范围水位，使旧的在途提取失效。"""
        record = self.get(access, memory_id)
        return self.repository.delete(record.id, record.scope, version)
