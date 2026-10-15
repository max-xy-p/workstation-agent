"""处理文本记忆的确定性规则，模型仅提供变更建议。"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from langchain_core.documents import Document

from ..retrieval.lexical import lexical_search
from ..tools.contracts import ToolError
from .models import MemoryAccess, MemoryRecord, MemoryScope
from .repository import MemoryRepository

_SECRET = re.compile(
    r"(?i)(?:\b(?:api[_-]?key|access[_-]?token|password|secret|authorization)"
    r"[\s\"']*[:=]\s*\S+|\bbearer\s+\S+|\bsk-[A-Za-z0-9_-]{16,}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----)"
)


def validate_content(text: str, secrets=()) -> str:
    """拒绝明显秘密和已知凭据，错误中不携带原文。"""
    if not text.strip() or len(text) > 4000:
        raise ToolError("记忆正文为空或超过长度限制", code="invalid_arguments")
    if _SECRET.search(text) or any(secret and secret in text for secret in secrets):
        raise ToolError("内容含敏感凭据，拒绝保存或提取", code="sensitive_content")
    return text.strip()


def redact(text: str, secrets=()) -> str:
    """为会话持久化移除已知凭据及常见秘密模式。"""
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[已脱敏]")
    return _SECRET.sub("[已脱敏]", text)


def terms(text):
    """为官方 BM25 提供英文词项与中文双字词，不实现排序算法。"""
    tokens = re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]+", text.casefold())
    return [
        part
        for token in tokens
        for part in (
            [token]
            if not re.fullmatch(r"[\u4e00-\u9fff]+", token) or len(token) == 1
            else [token[i : i + 2] for i in range(len(token) - 1)]
        )
    ]


class MemoryService:
    """通过仓库协议校验范围、内容和版本，不直接访问系统资源。"""

    def __init__(self, repository: MemoryRepository, *, max_records=2000, secrets=()):
        self.repository = repository
        self.max_records = max_records
        self.secrets = secrets

    def records(self, access: MemoryAccess, *, include_expired=False):
        """仅在可信范围内列出记录，默认排除过期内容。"""
        now = datetime.now(UTC)
        return tuple(
            record
            for record in self.repository.records(access.scopes)
            if include_expired or record.expires_at is None or record.expires_at > now
        )

    def get(self, access, memory_id, *, include_expired=True):
        """按 ID 读取也检查完整范围，不暴露其他所有者是否存在。"""
        record = next(
            (r for r in self.records(access, include_expired=include_expired) if r.id == memory_id),
            None,
        )
        if record is None:
            raise ToolError("记忆不存在或不在允许范围", code="not_found")
        return record

    def search(self, access, text, *, limit=5):
        """对已隔离的正文复用官方 BM25，排除没有词项证据的候选。"""
        query_terms = set(terms(text))
        records = self.records(access)
        matches = [r for r in records if query_terms.intersection(terms(r.content))]
        if not matches:
            return ()
        by_id = {r.id: r for r in matches}
        documents = lexical_search(
            [
                Document(id=r.id, page_content=r.content, metadata={"memory_id": r.id})
                for r in matches
            ],
            text,
            limit,
            terms,
        )
        return tuple(by_id[doc.metadata["memory_id"]] for doc in documents)

    def remember(self, access, *, content, kind, scope, source, expires_at=None):
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

    def update(self, access, memory_id, version, *, content, source, expiry="keep"):
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

    def save(self, access, record, *, expected_version=None, generation=None, batch_id=None):
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

    def commit(self, access, records, expected, scope: MemoryScope, **options):
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
        return self.repository.apply(records, expected, scope, **options)

    def forget(self, access, memory_id, version):
        """删除正文并提升范围水位，使旧的在途提取失效。"""
        record = self.get(access, memory_id)
        return self.repository.delete(record.id, record.scope, version)
