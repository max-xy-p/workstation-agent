"""声明会话及消息管理参数，不依赖 HTTP、数据库或模型 SDK。"""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

type SessionId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{1,128}$")]
type Title = Annotated[str, Field(min_length=1, max_length=200, pattern=r"\S")]
type Content = Annotated[str, Field(min_length=1, max_length=32_000, pattern=r"\S")]


class CreateSession(BaseModel):
    """创建空会话；身份和工作区只能来自服务配置。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    title: Title = "新会话"


class RenameSession(BaseModel):
    """携带读取时的版本号更名，避免覆盖其他调用方刚完成的修改。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    title: Title
    revision: int = Field(ge=0)


class WriteMessage(BaseModel):
    """保存或编辑用户文本；调用方不能伪造助手答复或工具消息。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    content: Content
    revision: int = Field(ge=0)


class PageRequest(BaseModel):
    """以有界页长读取列表；会话按更新时间倒序，消息按位置升序。"""

    model_config = ConfigDict(extra="forbid")
    limit: int = Field(default=50, ge=1, le=100)
    offset: int = Field(default=0, ge=0, le=100_000)


class Page[T](BaseModel):
    """返回本页数据与后续偏移量；并发修改后调用方应重新读取列表。"""

    items: list[T]
    next_offset: int | None
