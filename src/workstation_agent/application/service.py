"""通过官方 Agent 图执行任务，协调会话、Context 和独立记忆处理结果。"""

import asyncio
import json
import math
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from copy import deepcopy
from time import time
from typing import Literal, Protocol
from weakref import WeakValueDictionary

from langchain.agents import AgentState
from langchain.agents.middleware.model_call_limit import ModelCallLimitExceededError
from langchain.agents.middleware.tool_call_limit import ToolCallLimitExceededError
from langchain.agents.middleware.types import InputAgentState, OutputAgentState
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
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
        run = Run(task=task, session_id=session_id)
        if saved_control is not None:
            run.id = saved_control["run_id"]
        runtime.run_id = run.id
        control_state: RunControlState = saved_control or {
            "schema_version": 1,
            "run_id": run.id,
            "task": task,
            "deadline": time() + self.run_timeout,
            "closed": False,
        }
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

            class ExecutionInput(InputAgentState):
                run_control: RunControlState

            inputs: ExecutionInput = {
                "messages": [HumanMessage(content=task, id=run.id + "_user")],
                "run_control": control_state,
            }
            run.transition(RunStatus.THINKING)
            latest = None
            async with asyncio.timeout(max(0, control.deadline - time())):
                async for update in self.graph.astream(
                    resume if resume is not None else inputs,
                    config=config,
                    context=runtime,
                    stream_mode="updates",
                ):
                    for node, values in update.items():
                        if on_node:
                            on_node(node)
                        # 第三方流更新在此接入，业务处理只接受已构造的官方消息。
                        if not isinstance(values, dict):
                            continue
                        if runtime.board is not None and on_status:
                            on_status(deepcopy(runtime.board))
                        for message in values.get("messages", []):
                            if isinstance(message, AIMessage) and node == "model":
                                step = Step(run_id=run.id, kind=StepKind.MODEL, name="model")
                                step.finish()
                                run.steps.append(step)
                                latest = message
                            elif isinstance(message, ToolMessage) and node == "tools":
                                step = Step(
                                    run_id=run.id, kind=StepKind.TOOL, name=message.name or "tool"
                                )
                                if message.status == "error":
                                    step.fail("工具调用失败")
                                else:
                                    step.finish()
                                run.steps.append(step)
                snapshot = await self.graph.aget_state(config)
                if snapshot.interrupts:
                    run.transition(RunStatus.WAITING_APPROVAL)
                    run.error = "工具需要审批，请查看待审批列表后恢复运行"
                elif latest is None or latest.tool_calls:
                    run.fail("模型未生成最终答复")
                else:
                    extraction_step = await self._extract(run)
                    run.complete(latest.text)
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
        return run

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
