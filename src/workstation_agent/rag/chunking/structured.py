"""使用成熟 Markdown 解析器和官方分块器，适配原文位置及章节关联。"""

import re

from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter
from markdown_it import MarkdownIt

from ..config import RagSettings
from ..identifiers import stable_id
from ..models import SourceDocument
from .contracts import ChunkBudgetExceeded, ChunkingSignature


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
        sections = []
        protected = []
        stack = []
        section_start, heading = 0, ""
        for i, token in enumerate(tokens):
            if token.map is None:
                continue
            begin, end = (offsets[line] for line in token.map)
            if token.type == "heading_open" and token.level == 0:
                if begin > section_start:
                    sections.append((section_start, begin, heading))
                level = int(token.tag[1:])
                stack = [(depth, title) for depth, title in stack if depth < level]
                stack.append((level, tokens[i + 1].content))
                heading = " → ".join(title for _, title in stack)
                section_start = begin
            elif token.type in {"fence", "code_block", "table_open"}:
                auxiliary = (
                    "表头：" + text[begin:end].splitlines()[0].strip()
                    if token.type == "table_open"
                    else "代码语言：" + (token.info.strip() or "未指定")
                )
                protected.append((begin, end, auxiliary))
        sections.append((section_start, len(text), heading))
        chunks = []
        for section_start, section_end, heading in sections:
            section_id = stable_id("section", document.id, document.checksum, str(section_start))
            units = []
            cursor = section_start
            for begin, end, auxiliary in protected:
                if not section_start <= begin < section_end:
                    continue
                if cursor < begin:
                    units.append((cursor, begin, ""))
                units.append((begin, end, auxiliary))
                cursor = end
            if cursor < section_end:
                units.append((cursor, section_end, ""))
            section_chunks = []
            for begin, end, auxiliary in units:
                splitter = self.atomic_splitter if auxiliary else self.prose_splitter
                for part in splitter.create_documents([text[begin:end]]):
                    start = begin + part.metadata["start_index"]
                    stop = start + len(part.page_content)
                    # 框架偏移针对输入切片，必须先验证再转为全文位置，不能猜测重复文本位置。
                    if start < begin or stop > end or text[start:stop] != part.page_content:
                        raise ValueError("分块器返回的原文位置无效")
                    if not part.page_content.strip():
                        continue
                    if len(chunks) + len(section_chunks) >= self.settings.max_chunks:
                        raise ChunkBudgetExceeded("分块数量超过预算")
                    section_chunks.append(
                        Document(
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
                            page_content=part.page_content,
                            metadata={
                                "document_id": document.id,
                                "source_uri": document.source_uri,
                                "title": document.title,
                                "checksum": document.checksum,
                                "media_type": document.media_type,
                                "ordinal": len(chunks) + len(section_chunks),
                                "heading_path": heading or None,
                                "section_id": section_id,
                                "source_start": start,
                                "source_end": stop,
                                "line_start": text.count("\n", 0, start) + 1,
                                "line_end": text.count("\n", 0, max(start, stop - 1)) + 1,
                                "auxiliary_context": auxiliary,
                                "context_prefix": (
                                    f"文档：{document.title}。章节：{heading or '正文'}。"
                                ),
                                "context_provenance": self.version,
                            },
                        )
                    )
            for i, chunk in enumerate(section_chunks):
                chunks.append(
                    chunk.model_copy(
                        update={
                            "metadata": {
                                **chunk.metadata,
                                "previous_chunk_id": section_chunks[i - 1].id if i else None,
                                "next_chunk_id": section_chunks[i + 1].id
                                if i + 1 < len(section_chunks)
                                else None,
                            },
                        }
                    )
                )
        return tuple(chunks)
