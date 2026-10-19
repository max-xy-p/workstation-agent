"""使用成熟 Markdown 解析器和官方分块器，适配原文位置及章节关联。"""

import re
from dataclasses import dataclass

from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter
from markdown_it import MarkdownIt
from markdown_it.token import Token

from ..config import RagSettings
from ..identifiers import stable_id
from ..models import SourceDocument
from .contracts import ChunkBudgetExceeded, ChunkingSignature


@dataclass(frozen=True, slots=True)
class _Section:
    """保存解析器识别的章节区间和完整标题路径。"""

    start: int
    end: int
    heading: str


@dataclass(frozen=True, slots=True)
class _Unit:
    """保存正文或需要避免重叠切分的代码、表格区间。"""

    start: int
    end: int
    auxiliary: str = ""


def _auxiliary(token: Token, text: str, begin: int, end: int) -> str:
    if token.type == "table_open":
        return "表头：" + text[begin:end].splitlines()[0].strip()
    return "代码语言：" + (token.info.strip() or "未指定")


def _outline(
    tokens: list[Token], text: str, offsets: list[int]
) -> tuple[list[_Section], list[_Unit]]:
    """把 Markdown 的行位置转为章节和保护区间，不修改原文。"""
    sections: list[_Section] = []
    protected: list[_Unit] = []
    stack: list[tuple[int, str]] = []
    section_start, heading = 0, ""
    for index, token in enumerate(tokens):
        if token.map is None:
            continue
        begin, end = (offsets[line] for line in token.map)
        if token.type == "heading_open" and token.level == 0:
            if begin > section_start:
                sections.append(_Section(section_start, begin, heading))
            level = int(token.tag[1:])
            stack = [(depth, title) for depth, title in stack if depth < level]
            stack.append((level, tokens[index + 1].content))
            heading = " → ".join(title for _, title in stack)
            section_start = begin
        elif token.type in {"fence", "code_block", "table_open"}:
            protected.append(_Unit(begin, end, _auxiliary(token, text, begin, end)))
    sections.append(_Section(section_start, len(text), heading))
    return sections, protected


def _section_units(section: _Section, protected: list[_Unit]) -> list[_Unit]:
    """在同一章节内交替保留普通正文与代码、表格，避免遗漏原文。"""
    units: list[_Unit] = []
    cursor = section.start
    for unit in protected:
        if not section.start <= unit.start < section.end:
            continue
        if cursor < unit.start:
            units.append(_Unit(cursor, unit.start))
        units.append(unit)
        cursor = unit.end
    if cursor < section.end:
        units.append(_Unit(cursor, section.end))
    return units


def _link_chunks(chunks: list[Document]) -> list[Document]:
    """只为同章节的块建立前后关联，原始块内容与身份保持不变。"""
    return [
        chunk.model_copy(
            update={
                "metadata": {
                    **chunk.metadata,
                    "previous_chunk_id": chunks[index - 1].id if index else None,
                    "next_chunk_id": chunks[index + 1].id if index + 1 < len(chunks) else None,
                }
            }
        )
        for index, chunk in enumerate(chunks)
    ]


class StructuredChunkingStrategy:
    """将解析器行位置映射回原文，分块边界交给官方递归分块器。"""

    version = "native-document-v3"

    def __init__(self, settings: RagSettings) -> None:
        self.settings = settings
        self.parser = MarkdownIt("commonmark").enable("table")
        self.prose_splitter = RecursiveCharacterTextSplitter(
            chunk_size=settings.chunk_size,
            add_start_index=True,
            strip_whitespace=False,
            keep_separator="end",
            chunk_overlap=settings.chunk_overlap,
            separators=["\r\n\r\n", "\n\n", "\r\n", "\n", "。", "！", "？", ". ", "；", "; ", ""],
        )
        self.atomic_splitter = RecursiveCharacterTextSplitter(
            chunk_size=settings.chunk_size,
            add_start_index=True,
            strip_whitespace=False,
            keep_separator="end",
            chunk_overlap=0,
            separators=["\r\n", "\n", ""],
        )

    @property
    def signature(self) -> ChunkingSignature:
        """保留现有版本标记与参数顺序，默认索引不因接口调整而失效。"""
        return (self.version, self.settings.chunk_size, self.settings.chunk_overlap)

    def split(self, document: SourceDocument, text: str) -> tuple[Document, ...]:
        """返回原文片段与同章节关联；字符偏移使用 [start, end)。"""
        offsets = [0, *(match.end() for match in re.finditer(r"\r\n|\r|\n", text))]
        if offsets[-1] != len(text):
            offsets.append(len(text))
        tokens = self.parser.parse(text) if document.media_type == "text/markdown" else []
        sections, protected = _outline(tokens, text, offsets)
        chunks: list[Document] = []
        for section in sections:
            section_chunks: list[Document] = []
            for unit in _section_units(section, protected):
                section_chunks.extend(
                    self._split_unit(
                        document, text, section, unit, len(chunks) + len(section_chunks)
                    )
                )
            chunks.extend(_link_chunks(section_chunks))
        return tuple(chunks)

    def _split_unit(
        self, document: SourceDocument, text: str, section: _Section, unit: _Unit, ordinal: int
    ) -> list[Document]:
        """让官方分块器确定边界，校验位置和预算后再构造框架文档。"""
        splitter = self.atomic_splitter if unit.auxiliary else self.prose_splitter
        chunks: list[Document] = []
        for part in splitter.create_documents([text[unit.start : unit.end]]):
            start = unit.start + part.metadata["start_index"]
            stop = start + len(part.page_content)
            if start < unit.start or stop > unit.end or text[start:stop] != part.page_content:
                raise ValueError("分块器返回的原文位置无效")
            if not part.page_content.strip():
                continue
            if ordinal + len(chunks) >= self.settings.max_chunks:
                raise ChunkBudgetExceeded("分块数量超过预算")
            chunks.append(
                self._chunk(
                    document,
                    text,
                    section,
                    unit,
                    part.page_content,
                    start,
                    ordinal + len(chunks),
                )
            )
        return chunks

    def _chunk(
        self,
        document: SourceDocument,
        text: str,
        section: _Section,
        unit: _Unit,
        content: str,
        start: int,
        ordinal: int,
    ) -> Document:
        """将已验证的原文区间封装为 Document，保留已有块 ID 和索引格式。"""
        stop = start + len(content)
        return Document(
            id=stable_id(
                "chunk",
                document.id,
                document.checksum,
                self.version,
                str(self.settings.chunk_size),
                str(self.settings.chunk_overlap),
                str(start),
                str(stop),
            ),
            page_content=content,
            metadata={
                "document_id": document.id,
                "source_uri": document.source_uri,
                "title": document.title,
                "checksum": document.checksum,
                "media_type": document.media_type,
                "ordinal": ordinal,
                "heading_path": section.heading or None,
                "section_id": stable_id(
                    "section", document.id, document.checksum, str(section.start)
                ),
                "source_start": start,
                "source_end": stop,
                "line_start": text.count("\n", 0, start) + 1,
                "line_end": text.count("\n", 0, max(start, stop - 1)) + 1,
                "auxiliary_context": unit.auxiliary,
                "context_prefix": f"文档：{document.title}。章节：{section.heading or '正文'}。",
                "context_provenance": self.version,
            },
        )
