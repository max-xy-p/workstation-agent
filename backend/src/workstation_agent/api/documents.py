"""声明与工作台 SDK 无关的文档摄取输入，正文不经过聊天模型重新生成。"""

from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator


class IngestDocument(BaseModel):
    """提交一个外部来源的完整文本版本，原始文档仍由来源系统管理。"""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)
    source_uri: str = Field(min_length=1, max_length=2048)
    title: str = Field(min_length=1, max_length=200, pattern=r"\S")
    text: str = Field(min_length=1, repr=False)
    media_type: Literal["text/markdown", "text/plain"]
    source_version: str | None = Field(default=None, min_length=1, max_length=200)
    metadata: dict[str, str] = Field(default_factory=dict, max_length=32)

    @field_validator("source_uri")
    @classmethod
    def external_source(cls, value: str) -> str:
        """来源只作身份和引用，不发起 URL 读取；拒绝凭据、查询串及文件系统来源。"""
        parsed = urlparse(value)
        if (not parsed.scheme or not parsed.netloc or parsed.scheme == "file"
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ValueError("外部文档需要不含凭据的稳定来源 URI")
        return value

    @field_validator("metadata")
    @classmethod
    def bounded_metadata(cls, value: dict[str, str]) -> dict[str, str]:
        """限制标签体积，来源版本由独立字段提供，不能在标签中覆盖。"""
        if any(not key or len(key) > 100 or len(item) > 1000 for key, item in value.items()):
            raise ValueError("文档标签超过限制")
        if set(value) & {"source_uri", "document_id", "media_type", "source_version"}:
            raise ValueError("文档标签不能覆盖来源字段")
        return value
