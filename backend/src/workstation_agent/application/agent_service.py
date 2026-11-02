"""管理常驻服务中的任务句柄，Agent 执行与审批仍交给现有应用。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

from ..domain.task_status import TaskBoard
from ..domain.runs import TERMINAL_RUN_STATUSES, Run, RunStatus, Session, Step
from ..tools.approval import ApprovalRequest
from .service import AgentApplication
from .runs import RunApplication, RunPersistenceError
from ..api.conversations import Page, PageRequest

type RunOperation = Callable[
    [Callable[[str], None], Callable[[str], None], Callable[[TaskBoard], None]], Awaitable[Run]
]


class AgentServiceError(Exception):
    """向接口返回固定的错误类别和安全说明，不附带底层异常。"""

    def __init__(self, code: str, message: str, status: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


@dataclass(slots=True)
class ServiceRun:
    """向客户端返回运行详情，不暴露用于模型输入的完整任务内部状态。"""

    id: str
    session_id: str
    status: RunStatus = RunStatus.THINKING
    node: str | None = None
    task_status: TaskBoard | None = None
    answer: str | None = None
    error: str | None = None
    steps: list[Step] = field(default_factory=list)
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @classmethod
    def from_run(cls, run: Run) -> ServiceRun:
        """从持久化领域记录生成独立响应副本。"""
        return cls(id=run.id, session_id=run.session_id, status=run.status, node=run.node,
                   task_status=deepcopy(run.task_status), answer=run.answer, error=run.error,
                   steps=deepcopy(run.steps), updated_at=run.updated_at)


class AgentServiceApplication:
    """一次执行一个任务，提供提交、查询和控制入口，不引入任务队列或第二套循环。"""

    def __init__(self, application: AgentApplication, *, capacity: int = 128,
                 history: RunApplication | None = None) -> None:
        if capacity < 1:
            raise ValueError("服务保留的任务数量必须为正数")
        self.application: AgentApplication = application
        self.history: RunApplication | None = history
        self.capacity = capacity
        self._runs: dict[str, ServiceRun] = {}
        self._storage_failures: set[str] = set()
        self._worker: asyncio.Task[None] | None = None
        self._gate = asyncio.Lock()
        self._closed = False

    def get(self, run_id: str) -> ServiceRun:
        """优先读取持久化详情；内存仅保留本进程句柄和存储失败说明。"""
        cached = self._runs.get(run_id)
        if self.history is not None:
            saved = self.history.get(run_id)
            if saved is not None:
                if cached is not None and run_id in self._storage_failures:
                    return deepcopy(cached)
                return ServiceRun.from_run(saved)
            raise AgentServiceError("not_found", "运行记录不存在", 404)
        run = self._runs.get(run_id)
        if run is None:
            raise AgentServiceError("not_found", "当前服务中没有该任务记录", 404)
        return deepcopy(run)

    def list_runs(self, page: PageRequest, *, session_id: str | None = None) -> Page[ServiceRun]:
        """按更新时间分页返回历史；没有存储的测试实例只读取内存。"""
        if self.history is not None:
            saved = self.history.list_runs(page, session_id=session_id)
            return Page(items=[deepcopy(self._runs[run.id]) if run.id in self._storage_failures and run.id in self._runs
                               else ServiceRun.from_run(run) for run in saved.items], next_offset=saved.next_offset)
        values = sorted((run for run in self._runs.values() if session_id is None or run.session_id == session_id),
                        key=lambda run: (run.updated_at, run.id), reverse=True)
        return Page(items=deepcopy(values[page.offset:page.offset + page.limit]),
                    next_offset=page.offset + page.limit if len(values) > page.offset + page.limit else None)

    async def delete(self, run_id: str) -> None:
        """仅删除已结束运行；与运行启动和审批共用会话锁，不删除会话消息。"""
        if self.history is None:
            raise AgentServiceError("storage_unavailable", "运行历史存储未配置", 503)
        await self.application.delete_run(run_id)
        self._runs.pop(run_id, None)
        self._storage_failures.discard(run_id)

    async def submit(
        self,
        task: str,
        *,
        session_id: str | None = None,
        constraints: tuple[str, ...] = (),
        criteria: tuple[str, ...] = (),
        new_task: bool = False,
    ) -> ServiceRun:
        """启动任务并在获得真实 Run ID 后返回；客户端断开不会取消已接收的任务。"""
        session_id = session_id or Session().id

        async def execute(
            started: Callable[[str], None],
            node: Callable[[str], None],
            status: Callable[[TaskBoard], None],
        ) -> Run:
            return await self.application.run_task(
                task, session_id=session_id, constraints=constraints, criteria=criteria,
                new_task=new_task, on_started=started, on_node=node, on_status=status,
            )

        return await self._start(session_id, execute)

    async def approvals(self, session_id: str) -> dict[str, ApprovalRequest]:
        """从框架检查点读取待审批项，因此服务重启后仍可按会话查询。"""
        self._require_idle()
        async with self._gate:
            self._require_idle()
            return await self.application.pending_approvals(session_id)

    async def resume(
        self, session_id: str, decisions: dict[str, Literal["approve", "reject"]]
    ) -> ServiceRun:
        """恢复完整审批决定并返回原 Run ID，实际授权仍由原工具执行链检查。"""
        async def execute(
            started: Callable[[str], None],
            node: Callable[[str], None],
            status: Callable[[TaskBoard], None],
        ) -> Run:
            return await self.application.resume_task(
                session_id, decisions, on_started=started, on_node=node, on_status=status
            )

        return await self._start(session_id, execute)

    def cancel(self, run_id: str) -> bool:
        """请求协作取消正在执行的任务；等待审批的操作通过审批拒绝。"""
        run = self.get(run_id)
        if run.status in TERMINAL_RUN_STATUSES:
            return False
        if not self.application.cancel(run_id):
            raise AgentServiceError("not_running", "任务未在执行；待审批操作请提交拒绝决定")
        return True

    async def close(self) -> None:
        """停止接收新任务，等待活动任务完成取消，再由服务关闭模型和 MCP 连接。"""
        self._closed = True
        worker = self._worker
        if worker is not None:
            for run in self._runs.values():
                if self.application.cancel(run.id):
                    break
            else:
                worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    def _require_idle(self) -> None:
        """忙碌时立即拒绝，避免请求排队后意外执行或审批查询长期阻塞。"""
        if self._closed:
            raise AgentServiceError("service_stopping", "服务正在停止", 503)
        if self._worker is not None:
            raise AgentServiceError("agent_busy", "Agent 正在执行任务，请完成或取消后再提交")

    def _make_room(self) -> None:
        """持久化服务只淘汰缓存；没有存储的实例只淘汰已结束记录，保留审批入口。"""
        if len(self._runs) < self.capacity:
            return
        finished = [run for run in self._runs.values()
                    if self.history is not None or run.status in TERMINAL_RUN_STATUSES]
        if not finished:
            raise AgentServiceError("capacity_reached", "待审批任务过多，请先处理现有任务", 503)
        oldest = min(finished, key=lambda run: run.updated_at)
        del self._runs[oldest.id]
        self._storage_failures.discard(oldest.id)

    async def _start(self, session_id: str, operation: RunOperation) -> ServiceRun:
        self._require_idle()
        async with self._gate:
            self._require_idle()
            if not any(
                run.session_id == session_id and run.status == RunStatus.WAITING_APPROVAL
                for run in self._runs.values()
            ):
                self._make_room()
            ready: asyncio.Future[ServiceRun] = asyncio.get_running_loop().create_future()
            self._worker = asyncio.create_task(self._execute(session_id, operation, ready))
            # 提交请求取消时，已接收的工作仍由服务持有；读取异常避免遗留未消费的 Future。
            try:
                return deepcopy(await asyncio.shield(ready))
            except asyncio.CancelledError:
                ready.add_done_callback(self._consume_ready)
                raise

    @staticmethod
    def _consume_ready(ready: asyncio.Future[ServiceRun]) -> None:
        if not ready.cancelled():
            ready.exception()

    async def _execute(
        self, session_id: str, operation: RunOperation, ready: asyncio.Future[ServiceRun]
    ) -> None:
        run: ServiceRun | None = None

        def started(run_id: str) -> None:
            nonlocal run
            run = self.get(run_id) if self.history is not None else ServiceRun(id=run_id, session_id=session_id)
            self._runs[run_id] = run
            ready.set_result(run)

        def node(name: str) -> None:
            if run is not None:
                run.node = name
                run.updated_at = datetime.now(UTC)

        def status(board: TaskBoard) -> None:
            if run is not None:
                run.task_status = deepcopy(board)

        try:
            result = await operation(started, node, status)
            if run is not None:
                run.status = result.status
                run.answer, run.error = result.answer, result.error
                run.steps = deepcopy(result.steps)
        except asyncio.CancelledError:
            if run is not None:
                run.status = RunStatus.CANCELLED
            if not ready.done():
                ready.set_exception(AgentServiceError("service_stopping", "服务正在停止", 503))
            raise
        except Exception as exc:
            # 不把模型、存储或工具异常原文写入日志和 HTTP 响应。
            error = AgentServiceError("execution_failed", "任务启动或执行失败", 500)
            if isinstance(exc, ValueError):
                error = AgentServiceError("invalid_state", "会话或审批状态不符，请重新查询后操作")
            if isinstance(exc, RunPersistenceError):
                error = AgentServiceError("run_storage_failed", "运行记录保存失败，已停止继续执行", 503)
                if run is not None:
                    self._storage_failures.add(run.id)
            if not ready.done():
                ready.set_exception(error)
            if run is not None:
                run.status, run.error = RunStatus.FAILED, str(error)
        finally:
            if run is not None:
                run.updated_at = datetime.now(UTC)
            self._worker = None
