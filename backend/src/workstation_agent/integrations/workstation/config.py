"""定义可信工作台来源绑定，模型只能选择笔记，不能修改连接和工作区。"""

import os

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class WorkstationNotesConfig(BaseModel):
    """把笔记摄取工具固定到一个已配置的 MCP 服务和业务工作区，默认不启用。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    server: str = Field(pattern=r"^[a-z][a-z0-9_]{0,15}$")
    workspace_id: int = Field(gt=0)


def workstation_notes_from_env() -> WorkstationNotesConfig | None:
    """读取来源绑定，不连接业务服务；非法配置只返回固定说明。"""
    raw = os.getenv("WORKSTATION_AGENT_WORKSTATION_NOTES")
    if not raw:
        return None
    try:
        return WorkstationNotesConfig.model_validate_json(raw)
    except ValidationError:
        raise ValueError("WORKSTATION_AGENT_WORKSTATION_NOTES 必须指定有效的 server 和 workspace_id") from None
