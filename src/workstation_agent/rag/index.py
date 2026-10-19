"""定义原子发布数据、索引预处理和模型输出校验。"""

import math
import re
from dataclasses import dataclass

from langchain_core.documents import Document
from pydantic import BaseModel, ConfigDict

from .models import SourceDocument

# 该标记描述已保存的索引文本格式，切换查询策略不改变它，避免无必要地重建索引。
INDEX_TEXT_VERSION = "native-retrieval-v3"


def terms(text: str) -> list[str]:
    """保留技术标识符，中文使用单字与双字滑窗，索引和查询保持一致。"""
    result = []
    for match in re.finditer(
        r"\w+(?:[./:-]\w+)*|[\u3400-\u9fff]+", text.casefold(), flags=re.ASCII
    ):
        value = match[0]
        if re.fullmatch(r"[\u3400-\u9fff]+", value):
            result.extend(value)
            result.extend(value[i : i + 2] for i in range(len(value) - 1))
        else:
            result.append(value)
            result.extend(piece for piece in re.split(r"[_./:-]+", value) if piece != value)
    return result


def index_text(chunk: Document) -> str:
    """拼接派生信息供索引使用，原文 page_content 保持不变。"""
    return "\n".join(
        filter(
            None,
            (
                chunk.metadata.get("context_prefix"),
                chunk.metadata.get("auxiliary_context"),
                chunk.page_content,
            ),
        )
    )


class IndexSnapshot(BaseModel):
    """原子发布来源、框架文档及按块 ID 关联的向量，不保存重复词频。"""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    source: SourceDocument
    chunks: tuple[Document, ...]
    vectors: dict[str, tuple[float, ...]]
    fingerprint: str


@dataclass(frozen=True, slots=True)
class IndexState:
    """保存一次读取的有效快照和无法参与当前检索的来源。"""

    snapshots: tuple[IndexSnapshot, ...] = ()
    obsolete_sources: tuple[str, ...] = ()


def validate_snapshot(snapshot: IndexSnapshot):
    """拒绝来源混用、重复块 ID、原文区间错误及不完整向量映射。"""
    identifiers = [chunk.id for chunk in snapshot.chunks]
    if any(not identifier for identifier in identifiers) or len(set(identifiers)) != len(
        identifiers
    ):
        raise ValueError("块 ID 缺失或重复")
    for ordinal, chunk in enumerate(snapshot.chunks):
        metadata = chunk.metadata
        if (
            metadata["document_id"] != snapshot.source.id
            or metadata["source_uri"] != snapshot.source.source_uri
            or metadata["checksum"] != snapshot.source.checksum
            or metadata["ordinal"] != ordinal
            or metadata["source_start"] < 0
            or metadata["source_end"] - metadata["source_start"] != len(chunk.page_content)
        ):
            raise ValueError("原文片段与来源不一致")
    if snapshot.vectors:
        if set(snapshot.vectors) != set(identifiers):
            raise ValueError("向量与块 ID 不匹配")
        validate_vectors(list(snapshot.vectors.values()), len(snapshot.chunks))


def validate_vectors(vectors, count, dimension=None):
    """拒绝不完整、零向量、非有限数和维度不一致的模型输出。"""
    if len(vectors) != count:
        raise ValueError("embedding 数量不匹配")
    for vector in vectors:
        if not vector or any(type(v) not in (int, float) or not math.isfinite(v) for v in vector):
            raise ValueError("embedding 无效")
        dimension = dimension or len(vector)
        if len(vector) != dimension or sum(v * v for v in vector) == 0:
            raise ValueError("embedding 维度或长度无效")
    return dimension
