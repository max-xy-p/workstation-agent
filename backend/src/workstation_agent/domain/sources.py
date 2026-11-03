"""保存某次回答检索到的原文片段，不依赖当前知识库是否仍保留该版本。"""

from dataclasses import dataclass
from hashlib import sha256


def source_id(document_id: str, checksum: str, start: int, end: int) -> str:
    """为同一文档版本的同一原文范围生成稳定引用编号。"""
    return "src_" + sha256(f"{document_id}:{checksum}:{start}:{end}".encode()).hexdigest()[:24]


@dataclass(frozen=True, slots=True)
class AnswerSource:
    """记录回答可核对的证据；外部链接打开当前原笔记，片段保留检索时的版本。"""

    id: str
    document_id: str
    checksum: str
    source_uri: str
    title: str
    line_start: int
    line_end: int
    content: str
    citation: str
    source_version: str | None = None
    original_url: str | None = None
