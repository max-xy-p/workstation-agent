"""定义知识文档、文本片段和检索结果。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from ..domain.runs import new_id


@dataclass(frozen=True, slots=True)
class Document:
    """记录知识文档的来源、标题和版本信息。"""
    # 原始文档的地址或路径。
    source_uri: str
    # 文档标题。
    title: str
    # 标识内容版本的校验值，由调用方提供。
    checksum: str
    # 文档的唯一标识。
    id: str = field(default_factory=lambda: new_id("doc"))
    # 文档的 MIME 类型。
    media_type: str = "text/plain"
    # 记录创建时间，使用 UTC。
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    # 文档属性，用于检索过滤。
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class TextChunk:
    """保存文档中的可检索文本片段。"""
    # 所属文档标识。
    document_id: str
    # 片段在文档中的顺序编号。
    ordinal: int
    # 片段文本。
    content: str
    # 片段的唯一标识。
    id: str = field(default_factory=lambda: new_id("chunk"))
    # 片段所属的章节位置，可为空。
    heading_path: str | None = None
    # 片段附加属性，当前不参与过滤。
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RetrievalQuery:
    """定义知识检索文本、过滤条件和数量上限。"""
    # 知识检索文本。
    text: str
    # 最多返回的片段数。
    limit: int = 5
    # 文档属性的精确匹配条件。
    filters: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RetrievedChunk:
    """保存命中的文本片段、排序分数和来源。"""
    # 命中的文本片段。
    chunk: TextChunk
    # 排序分数，当前为关键词出现次数。
    score: float
    # 所属文档的来源地址。
    source_uri: str
    # 所属文档的标题。
    title: str
