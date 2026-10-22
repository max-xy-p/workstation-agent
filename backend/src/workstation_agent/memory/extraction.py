"""校验框架提取候选，构造可原子提交的文本记忆变更，不执行系统副作用。"""

import hashlib
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Protocol

from ..tools.contracts import ToolError
from .models import MemoryCandidate, MemoryRecord, MemoryScope, MemorySource


class MemoryExtractor(Protocol):
    """提取适配器仅返回领域候选，不暴露框架的 Store 或提取结果类型。"""

    def extract(
        self, text: str, existing: tuple[MemoryRecord, ...]
    ) -> tuple[MemoryCandidate, ...]: ...


def prepare_candidates(
    proposed: tuple[MemoryCandidate, ...],
    existing: Sequence[MemoryRecord],
    scope: MemoryScope,
    text: str,
    run_id: str,
    session_id: str,
) -> tuple[tuple[MemoryRecord, ...], dict[str, int | None], int]:
    """证据必须来自本次输入，自动处理不能覆盖显式记忆。"""
    by_id = {record.id: record for record in existing}
    contents = {record.content for record in existing}
    records: list[MemoryRecord] = []
    expected: dict[str, int | None] = {}
    pending = 0
    for index, proposal in enumerate(proposed):
        candidate = MemoryCandidate.model_validate(proposal)
        if not candidate.certain or candidate.evidence not in text:
            pending += 1
            continue
        old = _target_record(candidate, by_id)
        if old and any(source.origin == "explicit" for source in old.sources):
            pending += 1
            continue
        if candidate.content in contents:
            continue
        record = _prepared_record(candidate, old, scope, run_id, session_id, index)
        if record.id in expected:
            raise ToolError("提取重复修改同一记录", code="invalid_model_output")
        records.append(record)
        contents.add(record.content)
        expected[record.id] = old.version if old else None
    return tuple(records), expected, pending


def _target_record(
    candidate: MemoryCandidate, by_id: dict[str, MemoryRecord]
) -> MemoryRecord | None:
    """显式指定更新目标时必须匹配已有记录及其版本。"""
    target = candidate.target_id
    old = by_id.get(target) if target else None
    if target and (old is None or candidate.expected_version != old.version):
        raise ToolError("提取目标或版本无效", code="version_conflict")
    return old


def _prepared_record(
    candidate: MemoryCandidate,
    old: MemoryRecord | None,
    scope: MemoryScope,
    run_id: str,
    session_id: str,
    index: int,
) -> MemoryRecord:
    """构造带提取来源的新版本，保留旧记录的创建时间与到期设置。"""
    source = MemorySource(
        origin="extracted", run_id=run_id, session_id=session_id, message_id=run_id + "_user"
    )
    memory_id = (
        old.id
        if old
        else "memory_"
        + hashlib.sha256((scope.namespace + run_id + str(index)).encode()).hexdigest()[:32]
    )
    return MemoryRecord(
        id=memory_id,
        content=candidate.content,
        kind=candidate.kind,
        scope=scope,
        sources=(*old.sources, source) if old else (source,),
        version=old.version + 1 if old else 1,
        created_at=old.created_at if old else datetime.now(UTC),
        expires_at=old.expires_at if old else None,
    )
