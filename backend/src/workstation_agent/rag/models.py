"""记录知识来源身份；处理链中的片段直接使用 LangChain Document。"""

from dataclasses import dataclass, field
from datetime import UTC, datetime

from ..domain.runs import new_id


@dataclass(frozen=True, slots=True)
class SourceDocument:
    """保存来源身份和版本，区别于框架用于处理内容的 Document。"""

    source_uri: str
    title: str
    checksum: str
    id: str = field(default_factory=lambda: new_id("doc"))
    media_type: str = "text/plain"
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    metadata: dict[str, str] = field(default_factory=dict)
