"""在分块策略之外检查来源、原文和预算，所有策略共用同一套约束。"""

from langchain_core.documents import Document
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..config import RagSettings
from ..models import SourceDocument
from .contracts import ChunkBudgetExceeded, InvalidChunkingResult


class _ChunkMetadata(BaseModel):
    """校验框架 Document 的开放 metadata 字典，允许策略保留额外的辅助字段。"""

    model_config = ConfigDict(strict=True)

    document_id: str
    source_uri: str
    title: str
    checksum: str
    media_type: str
    ordinal: int = Field(ge=0)
    heading_path: str | None
    section_id: str = Field(min_length=1)
    source_start: int = Field(ge=0)
    source_end: int = Field(gt=0)
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)
    auxiliary_context: str
    context_prefix: str
    context_provenance: str = Field(min_length=1)
    previous_chunk_id: str | None
    next_chunk_id: str | None


def validate_chunks(
    document: SourceDocument,
    text: str,
    chunks: tuple[Document, ...],
    settings: RagSettings,
) -> None:
    """拒绝丢失正文、伪造位置、来源混用、错误邻接和超预算的分块结果。"""
    if len(chunks) > settings.max_chunks:
        raise ChunkBudgetExceeded("分块数量超过预算")
    identifiers = [chunk.id for chunk in chunks]
    if any(not identifier for identifier in identifiers) or len(set(identifiers)) != len(chunks):
        raise InvalidChunkingResult("块 ID 缺失或重复")
    try:
        # metadata 是框架公开的自由字典；在这里转换成有明确字段的对象再检查。
        metadata = [_ChunkMetadata.model_validate(chunk.metadata) for chunk in chunks]
    except ValidationError:
        raise InvalidChunkingResult("分块字段不符合约定") from None

    covered_end = 0
    previous_start = -1
    closed_sections: set[str] = set()
    previous_section: str | None = None
    for ordinal, (chunk, fields) in enumerate(zip(chunks, metadata, strict=True)):
        _validate_origin(document, fields, ordinal)
        _validate_position(chunk, fields, text, previous_start, covered_end, settings)
        _validate_section(fields, previous_section, closed_sections, covered_end)
        _validate_neighbors(chunks, metadata, ordinal)
        begin, end = fields.source_start, fields.source_end
        covered_end = end
        previous_start = begin
        previous_section = fields.section_id
    if text[covered_end:].strip():
        raise InvalidChunkingResult("分块遗漏正文")


def _validate_origin(document: SourceDocument, fields: _ChunkMetadata, ordinal: int) -> None:
    """策略不能更换文档来源或声明不同的块顺序。"""
    actual = (
        fields.document_id,
        fields.source_uri,
        fields.title,
        fields.checksum,
        fields.media_type,
        fields.ordinal,
    )
    expected = (
        document.id,
        document.source_uri,
        document.title,
        document.checksum,
        document.media_type,
        ordinal,
    )
    if actual != expected:
        raise InvalidChunkingResult("分块来源或顺序不一致")


def _validate_position(
    chunk: Document,
    fields: _ChunkMetadata,
    text: str,
    previous_start: int,
    covered_end: int,
    settings: RagSettings,
) -> None:
    """逐字核对原文及行号，并拒绝越界、倒序、遗漏与超长块。"""
    begin, end = fields.source_start, fields.source_end
    if len(chunk.page_content) > settings.chunk_size:
        raise ChunkBudgetExceeded("分块长度超过预算")
    if (
        not chunk.page_content.strip()
        or not 0 <= begin < end <= len(text)
        or begin < previous_start
        or end <= covered_end
        or text[begin:end] != chunk.page_content
        or fields.line_start != text.count("\n", 0, begin) + 1
        or fields.line_end != text.count("\n", 0, end - 1) + 1
    ):
        raise InvalidChunkingResult("分块原文或位置无效")
    if begin > covered_end and text[covered_end:begin].strip():
        raise InvalidChunkingResult("分块遗漏正文")


def _validate_section(
    fields: _ChunkMetadata, previous: str | None, closed: set[str], covered_end: int
) -> None:
    """章节只能连续出现，新的章节不能覆盖上一章节的正文。"""
    if fields.section_id == previous:
        return
    if fields.section_id in closed or fields.source_start < covered_end:
        raise InvalidChunkingResult("章节不连续或跨章节重叠")
    if previous is not None:
        closed.add(previous)


def _validate_neighbors(
    chunks: tuple[Document, ...], metadata: list[_ChunkMetadata], ordinal: int
) -> None:
    """前后引用只能指向同章节中实际相邻的块。"""
    fields = metadata[ordinal]
    before = metadata[ordinal - 1] if ordinal else None
    after = metadata[ordinal + 1] if ordinal + 1 < len(chunks) else None
    expected_previous = (
        chunks[ordinal - 1].id if before and before.section_id == fields.section_id else None
    )
    expected_next = (
        chunks[ordinal + 1].id if after and after.section_id == fields.section_id else None
    )
    if fields.previous_chunk_id != expected_previous or fields.next_chunk_id != expected_next:
        raise InvalidChunkingResult("分块邻接关系无效")
