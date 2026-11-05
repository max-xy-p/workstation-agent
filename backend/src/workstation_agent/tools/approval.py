"""校验 LangGraph 中断内容及应用提交的审批决定。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, JsonValue
from ..safety import PermissionRequired


class ApprovalRequest(BaseModel):
    """只描述一个待执行调用；ID 由官方检查点绑定，模型不能指定审批结果。"""

    model_config = ConfigDict(extra="forbid")
    name: str
    arguments: dict[str, JsonValue]
    fingerprint: str


class ApprovalDecision(BaseModel):
    """应用层根据用户选择提交决定，并再次绑定待执行参数。"""

    model_config = ConfigDict(extra="forbid")
    action: Literal["approve", "reject"]
    fingerprint: str


class PlannedApprovalRequired(PermissionRequired):
    """将已核对计划的摘要和审批指纹交给现有中断入口。"""

    def __init__(self, request: ApprovalRequest) -> None:
        super().__init__("操作计划需要审批")
        self.request: ApprovalRequest = request
