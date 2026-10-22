"""定义运行截止时间和协作式取消信号，不访问外部资源。"""

from dataclasses import dataclass, field
from threading import Event
from time import time


class ExecutionStopped(RuntimeError):
    """工具在开始操作前发现运行已取消或超过截止时间。"""


@dataclass(slots=True)
class ExecutionControl:
    """供模型和工具共享停止信号；已经完成的外部操作不能自动撤销。"""

    deadline: float
    cancelled: Event = field(default_factory=Event)

    def check(self) -> None:
        """阻止取消或超时后继续开始工作。"""
        if self.cancelled.is_set():
            raise ExecutionStopped("任务已取消")
        if time() >= self.deadline:
            raise ExecutionStopped("达到整次任务时间限制")
