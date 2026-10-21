"""管理常驻服务中的任务句柄，Agent 执行与审批仍交给现有应用。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

from ..context.state import TaskBoard
from ..domain.runs import TERMINAL_RUN_STATUSES, Run, RunStatus, Session, Step
from ..tools.approval import ApprovalRequest
from .service import AgentApplication

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
    """提供可查询的运行进度；记录只在当前服务进程内限量保留。"""

    id: str
    session_id: str
    status: RunStatus = RunStatus.THINKING
    node: str | None = None
    task_status: TaskBoard | None = None
    answer: str | None = None
    error: str | None = None
    steps: list[Step] = field(default_factory=list)
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class AgentServiceApplication:
    """一次执行一个任务，提供提交、查询和控制入口，不引入任务队列或第二套循环。"""

    def __init__(self, application: AgentApplication, *, capacity: int = 128) -> None:
        if capacity < 1:
            raise ValueError("服务保留的任务数量必须为正数")
        self.application: AgentApplication = application
        self.capacity = capacity
        self._runs: dict[str, ServiceRun] = {}
        self._worker: asyncio.Task[None] | None = None
        self._gate = asyncio.Lock()
        self._closed = False

    def get(self, run_id: str) -> ServiceRun:
        """返回运行记录的副本；已淘汰或重启前的记录返回不存在。"""
        run = self._runs.get(run_id)
        if run is None:
            raise AgentServiceError("not_found", "当前服务中没有该任务记录", 404)
        return deepcopy(run)

    def list_runs(self) -> list[ServiceRun]:
        """按最近更新时间返回本进程保留的运行记录。"""
        return deepcopy(sorted(self._runs.values(), key=lambda run: run.updated_at, reverse=True))

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
        """达到容量时淘汰最早结束的记录，待审批任务不会被自动删除。"""
        if len(self._runs) < self.capacity:
            return
        finished = [run for run in self._runs.values() if run.status in TERMINAL_RUN_STATUSES]
        if not finished:
            raise AgentServiceError("capacity_reached", "待审批任务过多，请先处理现有任务", 503)
        oldest = min(finished, key=lambda run: run.updated_at)
        del self._runs[oldest.id]

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
            run = ServiceRun(id=run_id, session_id=session_id)
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
            if not ready.done():
                ready.set_exception(error)
            if run is not None:
                run.status, run.error = RunStatus.FAILED, str(error)
        finally:
            if run is not None:
                run.updated_at = datetime.now(UTC)
            self._worker = None
