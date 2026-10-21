"""通过官方 Agent 图执行任务，协调会话、Context 和独立记忆处理结果。"""

import asyncio
import json
import math
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from copy import deepcopy
from time import time
from typing import Any, Literal, Protocol
from weakref import WeakValueDictionary

from langchain.agents import AgentState
from langchain.agents.middleware.model_call_limit import ModelCallLimitExceededError
from langchain.agents.middleware.tool_call_limit import ToolCallLimitExceededError
from langchain.agents.middleware.types import InputAgentState, OutputAgentState
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, RemoveMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.errors import GraphRecursionError, NodeCancelledError
from langgraph.graph import END
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command
from pydantic import TypeAdapter

from ..agent.rag_guard import RagSearchStopped
from ..context import ContextBuilder
from ..context.budget import ContextLimitExceeded
from ..context.state import ContextRuntime, RunControlState, TaskBoard, restore_board
from ..context.status import confirm_items
from ..domain.execution import ExecutionControl, ExecutionStopped
from ..domain.runs import Run, RunStatus, Session, Step, StepKind
from ..memory.models import MemorySource
from ..safety import PermissionRequired
from ..tools.approval import ApprovalDecision, ApprovalRequest
from ..tools.builtin.memory import MEMORY_SOURCE
from ..tools.execution import EXECUTION, execute_blocking
from ..tools.langchain import ToolService


class _ExecutionInput(InputAgentState):
    """在官方消息输入中附加由应用构造的运行控制记录。"""

    run_control: RunControlState


class RunRepository(Protocol):
    """业务任务存储只负责结果，不能替代框架检查点。"""

    def save(self, run: Run) -> None: ...


class SessionCheckpointer(Protocol):
    """应用只需要身份配置和会话互斥，不依赖 SQLite 或其他存储实现。"""

    def config(self, session_id: str) -> RunnableConfig: ...
    def lease(
        self, config: RunnableConfig, *, deleting: bool = False
    ) -> AbstractContextManager[None]: ...


class AgentApplication:
    """协调一次调用，框架负责循环与检查点，工具边界负责所有副作用。"""

    def __init__(
        self,
        graph: CompiledStateGraph[AgentState, ContextRuntime, InputAgentState, OutputAgentState],
        *,
        context: ContextBuilder | None = None,
        repository: RunRepository | None = None,
        checkpointer: SessionCheckpointer | None = None,
        extraction: ToolService | None = None,
        run_timeout: float = 300.0,
    ) -> None:
        if not math.isfinite(run_timeout) or run_timeout <= 0:
            raise ValueError("整次任务超时必须为正数")
        self.run_timeout = run_timeout
        self._active: dict[str, tuple[ExecutionControl, asyncio.Task[Run]]] = {}
        self.graph = graph
        self.context = context or ContextBuilder()
        self.repository = repository
        self.checkpointer = checkpointer
        self.extraction = extraction
        self._locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()

    async def run_task(
        self,
        task: str,
        *,
        session_id: str | None = None,
        on_node: Callable[[str], None] | None = None,
        on_status: Callable[[TaskBoard], None] | None = None,
        constraints: tuple[str, ...] = (),
        criteria: tuple[str, ...] = (),
        new_task: bool = False,
        on_started: Callable[[str], None] | None = None,
    ) -> Run:
        """支持显式会话续聊；未配置持久化时不接受假会话恢复。"""
        if not task.strip():
            raise ValueError("任务不能为空")
        if session_id and self.checkpointer is None:
            raise ValueError("会话持久化未启用")
        session_id = session_id or Session().id
        config = self._config(session_id)
        runtime = ContextRuntime(
            session_id=session_id,
            task=task,
            builder=self.context,
            constraints=constraints,
            criteria=criteria,
            new_task=new_task,
        )
        lock = self._locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            with self._lease(config):
                snapshot = await self.graph.aget_state(config)
                control = snapshot.values.get("run_control")
                if snapshot.interrupts and not (control and control.get("closed")):
                    raise ValueError("该会话有待审批操作，请先恢复或拒绝审批")
                return await self._run(
                    task, session_id, config, on_node, runtime, on_status, on_started=on_started
                )

    def _config(self, session_id: str) -> RunnableConfig:
        """持久会话使用身份隔离配置；临时运行只使用内存检查点。"""
        if self.checkpointer is not None:
            return self.checkpointer.config(session_id)
        return {"configurable": {"thread_id": session_id}}

    def _lease(self, config: RunnableConfig) -> AbstractContextManager[None]:
        """通过注入的会话接口互斥，临时会话仅由应用内锁保护。"""
        return self.checkpointer.lease(config) if self.checkpointer else nullcontext()

    def cancel(self, run_id: str) -> bool:
        """取消当前应用内正在执行的任务；调用方仍可捕获取消异常。"""
        active = self._active.get(run_id)
        if active is None:
            return False
        control, task = active
        if control.cancelled.is_set():
            return False
        control.cancelled.set()
        task.cancel()
        return True

    async def pending_approvals(self, session_id: str) -> dict[str, ApprovalRequest]:
        """列出指定会话的待审批工具，键是 LangGraph 分配的中断 ID。"""
        config = self._config(session_id)
        lock = self._locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            with self._lease(config):
                snapshot = await self.graph.aget_state(config)
                control = snapshot.values.get("run_control")
                if control and control.get("closed"):
                    return {}
                return {
                    item.id: ApprovalRequest.model_validate(item.value)
                    for item in snapshot.interrupts
                }

    async def resume_task(
        self,
        session_id: str,
        decisions: dict[str, Literal["approve", "reject"]],
        *,
        on_node: Callable[[str], None] | None = None,
        on_status: Callable[[TaskBoard], None] | None = None,
        on_started: Callable[[str], None] | None = None,
    ) -> Run:
        """用户决定必须覆盖当前全部中断，恢复时保留原运行身份和截止时间。"""
        config = self._config(session_id)
        lock = self._locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            with self._lease(config):
                snapshot = await self.graph.aget_state(config)
                control = TypeAdapter(RunControlState).validate_python(
                    snapshot.values.get("run_control")
                )
                pending = {
                    item.id: ApprovalRequest.model_validate(item.value)
                    for item in snapshot.interrupts
                }
                if control["closed"] or not pending or set(decisions) != set(pending):
                    raise ValueError("请重新查看审批列表，并逐项批准或拒绝全部待审批操作")
                answers = {
                    key: ApprovalDecision(
                        action=decisions[key], fingerprint=request.fingerprint
                    ).model_dump()
                    for key, request in pending.items()
                }
                runtime = ContextRuntime(
                    run_id=control["run_id"],
                    session_id=session_id,
                    task=control["task"],
                    builder=self.context,
                    board=restore_board(snapshot.values.get("task_board")),
                )
                return await self._run(
                    control["task"],
                    session_id,
                    config,
                    on_node,
                    runtime,
                    on_status,
                    resume=Command(resume=answers),
                    saved_control=control,
                    on_started=on_started,
                )

    async def confirm_status(
        self, session_id: str, *, task_id: str, revision: int, item_ids: tuple[str, ...]
    ) -> TaskBoard:
        """用户验收明确的状态版本；只修改状态栏，不授予工具权限。"""
        config = self._config(session_id)
        lock = self._locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            with self._lease(config):
                snapshot = await self.graph.aget_state(config)
                if snapshot.next:
                    raise ValueError("请等待运行结束后再确认任务项")
                board = restore_board(snapshot.values.get("task_board"))
                if board is None:
                    raise ValueError("该会话没有任务状态")
                updated = confirm_items(
                    board, task_id=task_id, revision=revision, item_ids=item_ids
                )
                await self.graph.aupdate_state(config, {"task_board": updated}, as_node="tools")
                await self.graph.aupdate_state(config, None, as_node=END)
                return updated

    async def _run(
        self,
        task: str,
        session_id: str,
        config: RunnableConfig,
        on_node: Callable[[str], None] | None,
        runtime: ContextRuntime,
        on_status: Callable[[TaskBoard], None] | None,
        *,
        resume: Command | None = None,
        saved_control: RunControlState | None = None,
        on_started: Callable[[str], None] | None = None,
    ) -> Run:
        run, control_state = self._prepare_run(task, session_id, runtime, saved_control)
        control = ExecutionControl(control_state["deadline"])
        current = asyncio.current_task()
        if current is None:
            raise RuntimeError("任务必须在异步应用中运行")
        self._active[run.id] = (control, current)
        execution_token = EXECUTION.set(control)
        token = MEMORY_SOURCE.set(
            MemorySource(
                origin="explicit",
                session_id=session_id,
                run_id=run.id,
                message_id=run.id + "_user",
            )
        )
        extraction_step: Step | None = None
        try:
            self._save(run)
            if on_started:
                on_started(run.id)
            run.transition(RunStatus.PREPARING)

            inputs: _ExecutionInput = {
                "messages": [HumanMessage(content=task, id=run.id + "_user")],
                "run_control": control_state,
            }
            run.transition(RunStatus.THINKING)
            async with asyncio.timeout(max(0, control.deadline - time())):
                extraction_step = await self._execute_graph(
                    resume if resume is not None else inputs,
                    config,
                    runtime,
                    run,
                    on_node,
                    on_status,
                )
        except PermissionRequired:
            run.fail("内部操作缺少授权，请检查会话或记忆配置")
        except (ModelCallLimitExceededError, ToolCallLimitExceededError, GraphRecursionError):
            run.fail("达到 Agent 调用次数限制", limit_reached=True)
        except (TimeoutError, ExecutionStopped):
            control.cancelled.set()
            run.fail("达到整次任务时间限制", limit_reached=True)
        except ContextLimitExceeded as exc:
            run.fail(str(exc), limit_reached=True)
        except RagSearchStopped:
            run.fail("重复 RAG 查询已停止，请明确缺失证据", limit_reached=True)
        except (asyncio.CancelledError, NodeCancelledError) as exc:
            control.cancelled.set()
            run.transition(RunStatus.CANCELLED)
            EXECUTION.reset(execution_token)
            execution_token = None
            await self._close_run(config, control_state)
            self._save(run)
            if isinstance(exc, NodeCancelledError):
                raise asyncio.CancelledError from None
            raise
        except Exception:
            run.fail("Agent 执行失败，请检查模型服务、工具连接和配置")
        finally:
            MEMORY_SOURCE.reset(token)
            if execution_token is not None:
                EXECUTION.reset(execution_token)
            self._active.pop(run.id, None)
        await self._finish_run(run, runtime, config, control_state, extraction_step, on_status)
        return run

    def _prepare_run(
        self,
        task: str,
        session_id: str,
        runtime: ContextRuntime,
        saved: RunControlState | None,
    ) -> tuple[Run, RunControlState]:
        """新任务创建身份与截止时间，恢复任务沿用检查点中的控制记录。"""
        run = Run(task=task, session_id=session_id)
        if saved is not None:
            run.id = saved["run_id"]
        runtime.run_id = run.id
        control: RunControlState = saved or {
            "schema_version": 1,
            "run_id": run.id,
            "task": task,
            "deadline": time() + self.run_timeout,
            "closed": False,
        }
        return run, control

    async def _execute_graph(
        self,
        inputs: InputAgentState | Command,
        config: RunnableConfig,
        runtime: ContextRuntime,
        run: Run,
        on_node: Callable[[str], None] | None,
        on_status: Callable[[TaskBoard], None] | None,
    ) -> Step | None:
        """消费官方执行图，分别处理审批暂停、缺失答复和正常结束。"""
        latest = await self._stream_graph(inputs, config, runtime, run, on_node, on_status)
        snapshot = await self.graph.aget_state(config)
        if snapshot.interrupts:
            run.transition(RunStatus.WAITING_APPROVAL)
            run.error = "工具需要审批，请查看待审批列表后恢复运行"
            return None
        if latest is None or latest.tool_calls:
            run.fail("模型未生成最终答复")
            return None
        extraction = await self._extract(run)
        run.complete(latest.text)
        return extraction

    async def _stream_graph(
        self,
        inputs: InputAgentState | Command,
        config: RunnableConfig,
        runtime: ContextRuntime,
        run: Run,
        on_node: Callable[[str], None] | None,
        on_status: Callable[[TaskBoard], None] | None,
    ) -> AIMessage | None:
        """只将正式模型和工具消息计入业务步骤，中间件更新用于通知。"""
        latest: AIMessage | None = None
        async for update in self.graph.astream(
            inputs, config=config, context=runtime, stream_mode="updates"
        ):
            for node, values in update.items():
                message = self._consume_update(node, values, run, runtime, on_node, on_status)
                if message is not None:
                    latest = message
        return latest

    def _consume_update(
        self,
        node: str,
        values: Any,
        run: Run,
        runtime: ContextRuntime,
        on_node: Callable[[str], None] | None,
        on_status: Callable[[TaskBoard], None] | None,
    ) -> AIMessage | None:
        """在框架流接入边界忽略非字段更新，再通知调用方并记录正式消息。"""
        if on_node:
            on_node(node)
        if not isinstance(values, dict):
            return None
        if runtime.board is not None and on_status:
            on_status(deepcopy(runtime.board))
        return self._record_messages(node, values, run)

    @staticmethod
    def _record_messages(node: str, values: dict[str, Any], run: Run) -> AIMessage | None:
        """框架流更新允许开放字段，在接入边界校验消息后再记录业务步骤。"""
        messages = TypeAdapter(list[AnyMessage | RemoveMessage]).validate_python(
            values.get("messages", [])
        )
        latest: AIMessage | None = None
        for message in messages:
            if isinstance(message, AIMessage) and node == "model":
                step = Step(run_id=run.id, kind=StepKind.MODEL, name="model")
                step.finish()
                run.steps.append(step)
                latest = message
            elif isinstance(message, ToolMessage) and node == "tools":
                step = Step(run_id=run.id, kind=StepKind.TOOL, name=message.name or "tool")
                if message.status == "error":
                    step.fail("工具调用失败")
                else:
                    step.finish()
                run.steps.append(step)
        return latest

    async def _finish_run(
        self,
        run: Run,
        runtime: ContextRuntime,
        config: RunnableConfig,
        control_state: RunControlState,
        extraction_step: Step | None,
        on_status: Callable[[TaskBoard], None] | None,
    ) -> None:
        """保存收尾状态、请求用量和提取结果，最后通知调用方。"""
        if run.status != RunStatus.WAITING_APPROVAL:
            await self._close_run(config, control_state)
        for usage in runtime.usages:
            step = Step(run_id=run.id, kind=StepKind.RETRIEVAL, name="context_usage")
            step.finish(json.dumps(usage, ensure_ascii=False))
            run.steps.append(step)
            if usage["sources"]:
                step = Step(run_id=run.id, kind=StepKind.MEMORY, name="context_sources")
                step.finish(json.dumps({"sources": usage["sources"]}, ensure_ascii=False))
                run.steps.append(step)
        if extraction_step is not None:
            run.steps.append(extraction_step)
        if runtime.board is not None:
            if run.error:
                runtime.board["blockers"] = [*runtime.board["blockers"], run.error][-8:]
            if on_status:
                on_status(deepcopy(runtime.board))
        self._save(run)

    async def _close_run(self, config: RunnableConfig, control: RunControlState) -> None:
        """保存终止标记、补齐被中断的工具结果，并用官方接口清除待执行节点。"""
        snapshot = await self.graph.aget_state(config)
        messages = snapshot.values.get("messages", [])
        pending: dict[str, str] = {}
        for message in messages:
            if isinstance(message, AIMessage):
                for call in message.tool_calls:
                    if call["id"]:
                        pending[call["id"]] = call["name"]
            elif isinstance(message, ToolMessage):
                pending.pop(message.tool_call_id, None)
        stopped = [
            ToolMessage(
                "运行已停止，该调用未取得最终结果；重试前请核对实际操作是否发生",
                name=name,
                tool_call_id=identifier,
                status="error",
            )
            for identifier, name in pending.items()
        ]
        # tools 是官方 create_agent 的节点名；明确指定，避免并行节点停止后的推断歧义。
        await self.graph.aupdate_state(
            config,
            {"run_control": {**control, "closed": True}, "messages": stopped},
            as_node="tools",
        )
        await self.graph.aupdate_state(config, None, as_node=END)

    async def _extract(self, run: Run) -> Step | None:
        """在整次任务预算内提取记忆；普通提取失败只记录步骤。"""
        if self.extraction is None:
            return None
        extraction = self.extraction
        step = Step(run_id=run.id, kind=StepKind.MEMORY, name="memory_extraction")
        try:
            result = await execute_blocking(
                lambda: extraction.execute_tool(
                    "extract_memory",
                    {"text": run.task, "run_id": run.id, "session_id": run.session_id},
                )
            )
            if result.success:
                step.finish(result.content)
            else:
                step.fail("记忆提取未提交：" + (result.error_code or "memory_failed"))
        except ExecutionStopped:
            raise
        except Exception:
            step.fail("记忆提取失败；主任务结果已保留")
        return step

    def _save(self, run: Run) -> None:
        """仅在显式注入业务存储时保存，不将 Run 当成框架恢复状态。"""
        if self.repository is not None:
            self.repository.save(run)
