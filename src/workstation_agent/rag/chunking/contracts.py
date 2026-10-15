"""约定分块输入、输出和版本信息，应用用例通过该协议替换分块方式。"""

from typing import Protocol

from langchain_core.documents import Document

from ..models import SourceDocument

ChunkingSignature = tuple[str | int | float | bool, ...]


class ChunkBudgetExceeded(ValueError):
    """分块数量或长度超过配置预算，调用方不得发布部分结果。"""


class InvalidChunkingResult(ValueError):
    """策略输出不满足来源、位置或片段关联约定，禁止进入索引。"""


class ChunkingStrategy(Protocol):
    """将已读取的文本分为原文片段；实现不得自行读文件或调用外部模型。"""

    @property
    def signature(self) -> ChunkingSignature:
        """返回策略名称、版本及影响分块结果的参数，用于识别需要重建的索引。"""
        ...

    def split(self, document: SourceDocument, text: str) -> tuple[Document, ...]:
        """按原文顺序返回片段，保留来源、字符区间、行号及同章节邻接关系。"""
        ...
