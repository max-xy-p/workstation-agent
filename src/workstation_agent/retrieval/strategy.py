"""定义检索策略接口；调用方负责权限过滤，策略负责召回和排序。"""

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, TypeVar

from ..tools.contracts import ToolError

CandidateT = TypeVar("CandidateT", contravariant=True)


@dataclass(frozen=True, slots=True)
class RetrievalHit:
    """用候选 ID 和相关性分数表示命中，不携带可替换原文的内容。"""

    id: str
    score: float


class RetrievalStrategy(Protocol[CandidateT]):
    """仅检索调用方提供的候选；额外模型调用必须使用已授权的适配器。"""

    def retrieve(
        self, query: str, candidates: Sequence[CandidateT], *, limit: int
    ) -> tuple[RetrievalHit, ...]: ...


def validate_hits(
    hits: Sequence[RetrievalHit], allowed_ids: set[str], limit: int
) -> tuple[RetrievalHit, ...]:
    """拒绝未知 ID、重复 ID 和无效分数，避免替换策略后突破候选范围。"""
    seen: set[str] = set()
    for hit in hits:
        if hit.id not in allowed_ids or hit.id in seen or not math.isfinite(hit.score):
            raise ToolError("检索策略返回了无效结果", code="invalid_retrieval_output")
        seen.add(hit.id)
    return tuple(hits[:limit])
