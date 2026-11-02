"""通过授权入口管理运行历史，数据库访问始终交给注入的存储协议。"""

from collections.abc import Callable

from ..api.conversations import Page, PageRequest
from ..domain.runs import Run
from ..safety.permissions import ApprovalPolicy, PermissionLevel
from ..storage.base import RunRepository
from ..tools.contracts import ToolError
from ..tools.effects import authorized_operation
from ..tools.execution import EXECUTION


class RunPersistenceError(ToolError):
    """表示执行记录无法可靠提交，应用必须停止继续执行任务。"""

    def __init__(self) -> None:
        super().__init__("运行记录保存失败，已停止继续执行", code="run_storage_failed")


class RunApplication:
    """保存已授权任务的轨迹，读取和删除只作用于存储绑定的可信身份范围。"""

    def __init__(self, repository: RunRepository, policy: ApprovalPolicy) -> None:
        self.repository: RunRepository = repository
        self.policy: ApprovalPolicy = policy

    def _read[T](self, operation: Callable[[], T]) -> T:
        return authorized_operation("run_read", PermissionLevel.SAFE_READ, self.policy, operation)

    def initialize(self) -> None:
        """服务启动时授权执行版本迁移，再开放历史查询。"""
        authorized_operation("run_write", PermissionLevel.CONTROLLED_WRITE, self.policy,
                             self.repository.initialize)

    def save(self, run: Run) -> None:
        """提交关键进度；取消和超时后仍需保存收尾结果，失败向应用传播固定错误。"""
        token = EXECUTION.set(None)
        try:
            authorized_operation("run_write", PermissionLevel.CONTROLLED_WRITE, self.policy,
                                 lambda: self.repository.save(run))
        except ToolError:
            raise RunPersistenceError() from None
        finally:
            EXECUTION.reset(token)

    def get(self, run_id: str) -> Run | None:
        """读取一条当前身份可见的运行；不存在时返回 None。"""
        return self._read(lambda: self.repository.get(run_id))

    def list_runs(self, page: PageRequest, *, session_id: str | None = None) -> Page[Run]:
        """分页读取全部历史或指定会话的历史。"""
        return self._read(lambda: self.repository.list_runs(page, session_id=session_id))

    def unfinished(self) -> list[Run]:
        """供服务启动时核对失去执行进程的运行。"""
        return self._read(self.repository.unfinished)

    def delete(self, run_id: str) -> None:
        """由持有会话锁的应用调用，删除运行不删除用户消息或已发生的工具操作。"""
        authorized_operation("run_delete", PermissionLevel.DESTRUCTIVE, self.policy,
                             lambda: self.repository.delete(run_id))
