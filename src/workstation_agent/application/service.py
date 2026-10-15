"""通过官方 Agent 图执行任务，协调会话、Context 和独立记忆处理结果。"""

import asyncio
import json
from collections.abc import Callable
from weakref import WeakValueDictionary

from langchain.agents.middleware.model_call_limit import ModelCallLimitExceededError
from langchain.agents.middleware.tool_call_limit import ToolCallLimitExceededError
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.errors import GraphRecursionError

from ..agent.prompts import SYSTEM_PROMPT
from ..agent.rag_guard import RagSearchStopped
from ..context import ContextBuilder
from ..domain.runs import Run, RunStatus, Session, Step, StepKind
from ..memory.models import MemorySource
from ..safety import PermissionRequired
from ..tools.builtin.memory import MEMORY_SOURCE


class AgentApplication:
    """协调一次调用，框架负责循环与检查点，工具边界负责所有副作用。"""

    def __init__(self, graph, *, context=None, repository=None, checkpointer=None, extraction=None):
        self.graph = graph
        self.context = context or ContextBuilder()
        self.repository = repository
        self.checkpointer = checkpointer
        self.extraction = extraction
        self._locks = WeakValueDictionary()

    async def run_task(
        self,
        task: str,
        *,
        session_id: str | None = None,
        on_node: Callable[[str], None] | None = None,
    ) -> Run:
        """支持显式会话续聊；未配置持久化时不接受假会话恢复。"""
        if not task.strip():
            raise ValueError("任务不能为空")
        if session_id and self.checkpointer is None:
            raise ValueError("会话持久化未启用")
        session_id = session_id or Session().id
        config = self.checkpointer.config(session_id) if self.checkpointer else None
        lock = self._locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            if self.checkpointer:
                with self.checkpointer.lease(config):
                    return await self._run(task, session_id, config, on_node)
            return await self._run(task, session_id, config, on_node)

    async def _run(self, task, session_id, config, on_node):
        run = Run(task=task, session_id=session_id)
        token = MEMORY_SOURCE.set(
            MemorySource(
                origin="explicit",
                session_id=session_id,
                run_id=run.id,
                message_id=run.id + "_user",
            )
        )
        try:
            self._save(run)
            run.transition(RunStatus.PREPARING)
            messages, sources = await asyncio.to_thread(self.context.build, task, SYSTEM_PROMPT)
            evidence = "\n\n".join(
                m.content for m in messages if m.metadata.get("kind") == "context"
            )
            if sources.memory_ids or sources.chunk_ids:
                step = Step(run_id=run.id, kind=StepKind.MEMORY, name="context_sources")
                step.finish(
                    json.dumps({"memory_ids": sources.memory_ids, "chunk_ids": sources.chunk_ids})
                )
                run.steps.append(step)
            inputs = {"messages": [HumanMessage(content=task, id=run.id + "_user")]}
            run.transition(RunStatus.THINKING)
            latest = None
            async for update in self.graph.astream(
                inputs,
                config=config,
                context={"evidence": evidence},
                stream_mode="updates",
            ):
                for node, values in update.items():
                    if on_node:
                        on_node(node)
                    for message in (values or {}).get("messages", []):
                        if isinstance(message, AIMessage):
                            step = Step(run_id=run.id, kind=StepKind.MODEL, name="model")
                            step.finish()
                            run.steps.append(step)
                            latest = message
                        elif isinstance(message, ToolMessage):
                            step = Step(
                                run_id=run.id, kind=StepKind.TOOL, name=message.name or "tool"
                            )
                            if message.status == "error":
                                step.fail("工具调用失败")
                            else:
                                step.finish()
                            run.steps.append(step)
            if latest is None or latest.tool_calls:
                run.fail("模型未生成最终答复")
            else:
                run.complete(latest.text)
        except PermissionRequired:
            run.transition(RunStatus.WAITING_APPROVAL)
            run.error = "工具需要审批；当前入口尚未提供审批恢复"
        except (ModelCallLimitExceededError, ToolCallLimitExceededError, GraphRecursionError):
            run.fail("达到 Agent 调用次数限制", limit_reached=True)
        except RagSearchStopped:
            run.fail("重复 RAG 查询已停止，请明确缺失证据", limit_reached=True)
        except asyncio.CancelledError:
            run.transition(RunStatus.CANCELLED)
            self._save(run)
            raise
        except Exception:
            run.fail("Agent 执行失败，请检查模型服务、工具连接和配置")
        finally:
            MEMORY_SOURCE.reset(token)
        if self.extraction is not None and run.status == RunStatus.COMPLETED:
            step = Step(run_id=run.id, kind=StepKind.MEMORY, name="memory_extraction")
            run.steps.append(step)
            try:
                result = await asyncio.to_thread(
                    self.extraction.execute_tool,
                    "extract_memory",
                    {
                        "text": task,
                        "run_id": run.id,
                        "session_id": session_id,
                    },
                )
                if result.success:
                    step.finish(result.content)
                else:
                    step.fail("记忆提取未提交：" + (result.error_code or "memory_failed"))
            except Exception:
                step.fail("记忆提取失败；主任务结果已保留")
        self._save(run)
        return run

    def _save(self, run):
        """仅在显式注入业务存储时保存，不将 Run 当成框架恢复状态。"""
        if self.repository is not None:
            self.repository.save(run)
