"""定义可信工作台来源绑定，模型只能选择笔记，不能修改连接和工作区。"""

import os
import re
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

WORKSTATION_SDK_VERSION = "5.3.0"


class WorkstationNotesConfig(BaseModel):
    """把笔记摄取工具固定到一个已配置的 MCP 服务和业务工作区，默认不启用。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    server: str = Field(pattern=r"^[a-z][a-z0-9_]{0,15}$")
    workspace_id: int = Field(gt=0)
    web_url: str | None = Field(default=None, max_length=2048)
    expose_get_note: bool = True

    @field_validator("web_url")
    @classmethod
    def validate_web_url(cls, value: str | None) -> str | None:
        """工作台页面地址只允许不带凭据、查询串和片段的 HTTP 地址，不用于后台网络请求。"""
        if value is None:
            return None
        parsed = urlsplit(value)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.query or parsed.fragment or "\\" in value
                or any(ord(character) <= 32 for character in value)):
            raise ValueError("web_url 必须是不含凭据和查询参数的工作台 HTTP 地址")
        return value.rstrip("/")

    def note_url(self, source_uri: str) -> str | None:
        """仅为当前绑定工作区的明确笔记来源生成链接，不访问工作台或传递认证令牌。"""
        match = re.fullmatch(rf"workstation://{re.escape(self.server)}/workspaces/{self.workspace_id}/notes/([1-9][0-9]*)", source_uri)
        if self.web_url is None or match is None:
            return None
        return f"{self.web_url}/w/{self.workspace_id}/notes/{match[1]}/edit"


def workstation_notes_from_env() -> WorkstationNotesConfig | None:
    """读取来源绑定，不连接业务服务；非法配置只返回固定说明。"""
    raw = os.getenv("WORKSTATION_AGENT_WORKSTATION_NOTES")
    if not raw:
        return None
    try:
        return WorkstationNotesConfig.model_validate_json(raw)
    except ValidationError:
        raise ValueError("WORKSTATION_AGENT_WORKSTATION_NOTES 必须指定有效的 server 和 workspace_id") from None
