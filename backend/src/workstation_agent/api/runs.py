"""声明任务提交和审批决定，不依赖具体通信协议。"""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from .conversations import Content, SessionId, PageRequest


class RunQuery(PageRequest):
    """分页查询当前身份可见的运行，可进一步限制到一个会话。"""

    session_id: SessionId | None = None


class SubmitTask(BaseModel):
    """只接受任务与会话信息，调用方不能覆盖身份、模型地址或权限配置。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    task: Content
    session_id: SessionId | None = None
    constraints: list[Annotated[str, Field(min_length=1, max_length=2000)]] = Field(
        default_factory=list, max_length=32,
    )
    criteria: list[Annotated[str, Field(min_length=1, max_length=2000)]] = Field(
        default_factory=list, max_length=32,
    )
    new_task: bool = False


class ResumeTask(BaseModel):
    """提交全部待审批项的决定，授权参数始终取自可信检查点。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    decisions: dict[str, Literal["approve", "reject"]] = Field(min_length=1, max_length=32)
