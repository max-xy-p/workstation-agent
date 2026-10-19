"""捕获实际模型请求，验证预算、压缩、刷新和任务状态的关键边界。"""

from __future__ import annotations

import asyncio
import json
import unittest
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import AsyncMock, patch

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatResult
from langchain_core.tools import StructuredTool
from langgraph.errors import NodeCancelledError
from pydantic import BaseModel, Field

from tests.test_agent import ScriptedModel
from tests.unit.test_context_budget import fragment
from workstation_agent.agent.factory import build_agent
from workstation_agent.application import AgentApplication
from workstation_agent.bootstrap import build_memory_application
from workstation_agent.config import Settings
from workstation_agent.context import ContextBuilder
from workstation_agent.context.budget import ContextLimitExceeded, RequestCounter
from workstation_agent.context.config import ContextSettings
from workstation_agent.context.evidence import assemble_evidence
from workstation_agent.context.selection import normalize, validate_pairs
from workstation_agent.context.state import ContextRuntime, restore_board
from workstation_agent.context.status import collect_results, initialize, invalidate_memory_sources
from workstation_agent.tools import ReadTextFileTool, ToolError
from workstation_agent.tools.executor import ToolResult
from workstation_agent.tools.langchain import local_tools
from workstation_agent.tools.task_status import UpdateStatusArguments, UpdateTaskStatusTool


class CapturingModel(ScriptedModel):
    """记录官方 Agent 实际传给模型的消息与输出参数，不发送网络请求。"""

    requests: list[list[BaseMessage]] = Field(default_factory=list, exclude=True)
    settings_seen: list[dict[str, Any]] = Field(default_factory=list, exclude=True)

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        self.requests.append(deepcopy(messages))
        self.settings_seen.append(kwargs)
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)


def call(name: str, args: dict[str, Any], identifier: str) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": name,
                "args": args,
                "id": identifier,
                "type": "tool_call",
            }
        ],
    )


class ContextManagementTests(unittest.IsolatedAsyncioTestCase):
    async def test_output_reservation_and_request_accounting_reach_actual_model(self) -> None:
        model = CapturingModel(responses=[AIMessage(content="完成")])
        tool = StructuredTool.from_function(
            lambda text: text,
            name="large_schema",
            description="说明" * 800,
        )
        settings = ContextSettings(window=16000, output_tokens=600, safety_tokens=200)
        app = AgentApplication(build_agent(model, [tool], context_settings=settings))
        result = await app.run_task("回答问题", constraints=("使用中文",), criteria=("给出结论",))
        self.assertEqual(result.status.value, "completed", result.error)
        usage = json.loads(
            next(s.output_summary for s in result.steps if s.name == "context_usage")
        )
        self.assertGreater(usage["tool_schema_tokens"], 2000)
        self.assertGreater(usage["system_tokens"], 0)
        self.assertGreater(usage["data_tokens"], 0)
        self.assertEqual(model.settings_seen[0]["max_tokens"], 600)
        self.assertLessEqual(usage["input_tokens"], settings.input_budget())
        self.assertIn("使用中文", repr(model.requests[0]))
        self.assertEqual(usage["counter"], "approximate_utf8_upper_bound")

    async def test_oversized_current_user_or_system_stops_before_model(self) -> None:
        for huge_system in (False, True):
            model = CapturingModel(responses=[AIMessage(content="不应调用")])
            with patch(
                "workstation_agent.agent.factory.SYSTEM_PROMPT",
                "系统" * 9000 if huge_system else "系统规则",
            ):
                app = AgentApplication(
                    build_agent(model, [], context_settings=ContextSettings(window=8000))
                )
                result = await app.run_task("当前要求" if huge_system else "用户要求" * 9000)
            self.assertEqual(result.error, "context_limit_exceeded")
            self.assertEqual(model.requests, [])

    async def test_no_session_clears_old_results_and_preserves_current_request_and_pairs(
        self,
    ) -> None:
        model = CapturingModel(
            responses=[
                call("large", {}, "one"),
                call("large", {}, "two"),
                AIMessage(content="完成"),
            ]
        )
        tool = StructuredTool.from_function(
            lambda: "正文" * 700, name="large", description="读取数据"
        )
        settings = ContextSettings(
            window=15000, output_tokens=500, safety_tokens=200, trigger_ratio=0.6
        )
        graph = build_agent(model, [tool], context_settings=settings)
        result = await AgentApplication(graph).run_task("当前用户的具体要求必须原样保留")
        self.assertEqual(result.status.value, "completed", result.error)
        self.assertEqual(len(model.requests), 3)
        last = model.requests[-1][1:]
        validate_pairs(last)
        self.assertIn("当前用户的具体要求必须原样保留", [m.content for m in last])
        self.assertTrue(any("旧工具正文已清理" in m.text for m in last))
        self.assertLessEqual(
            RequestCounter(model).messages(model.requests[-1]), settings.input_budget()
        )

    async def test_necessary_latest_tool_result_overflow_does_not_send_second_request(self) -> None:
        model = CapturingModel(responses=[call("large", {}, "one"), AIMessage(content="不应调用")])
        tool = StructuredTool.from_function(lambda: "大" * 20000, name="large", description="读取")
        result = await AgentApplication(
            build_agent(model, [tool], context_settings=ContextSettings(window=9000))
        ).run_task("读取")
        self.assertEqual(result.error, "context_limit_exceeded")
        self.assertEqual(len(model.requests), 1)

    async def test_state_tool_uses_framework_state_and_rejects_unproven_completion(self) -> None:
        model = CapturingModel(
            responses=[
                call(
                    "update_task_status",
                    {"items": [{"id": "one", "text": "任务", "status": "completed"}]},
                    "s1",
                ),
                call(
                    "update_task_status",
                    {
                        "items": [{"id": "one", "text": "任务", "status": "in_progress"}],
                        "next_action": "继续处理",
                    },
                    "s2",
                ),
                AIMessage(content="继续"),
            ]
        )
        snapshots = []
        result = await AgentApplication(build_agent(model, [])).run_task(
            "检查项目",
            constraints=("不可改动文件",),
            on_status=lambda b: snapshots.append(deepcopy(b)),
        )
        self.assertEqual(result.status.value, "completed", result.error)
        self.assertTrue(
            any(
                step.status.value == "failed"
                for step in result.steps
                if step.name == "update_task_status"
            )
        )
        self.assertEqual(snapshots[-1]["items"][0]["status"], "in_progress")
        self.assertEqual(snapshots[-1]["goal"], "检查项目")
        self.assertEqual(snapshots[-1]["constraints"], ["不可改动文件"])

    async def test_memory_deletion_invalidates_candidates_before_next_model_request(self) -> None:
        with TemporaryDirectory() as directory:
            settings = Settings(Path(directory))
            memory = build_memory_application(settings, approve_write=True, approve_delete=True)
            record = memory.execute("remember_memory", {"content": "Python 项目使用特定旧配置"})
            model = CapturingModel(
                responses=[
                    call(
                        "delete_memory",
                        {"memory_id": record["id"], "expected_version": 1},
                        "delete",
                    ),
                    AIMessage(content="已删除"),
                ]
            )
            result = await AgentApplication(
                build_agent(model, local_tools(memory.tools)),
                context=ContextBuilder(memory=memory),
            ).run_task("Python 项目特定配置，删除这条记忆")
            self.assertEqual(result.status.value, "completed", result.error)
            self.assertIn("特定旧配置", repr(model.requests[0]))
            self.assertNotIn("特定旧配置", repr(model.requests[1]))
            self.assertEqual(memory.search("Python"), ())

    async def test_summary_failure_is_bounded_and_falls_back_to_legal_requests(self) -> None:
        model = CapturingModel(responses=[call("ping", {}, "ping"), AIMessage(content="完成")])
        tool = StructuredTool.from_function(lambda: "ok", name="ping", description="检查")
        settings = ContextSettings(window=16000, trigger_ratio=0.25)
        graph = build_agent(
            model,
            [tool],
            context_settings=settings,
            summary_model=ScriptedModel(responses=[AIMessage(content="摘要")]),
        )
        runtime = ContextRuntime(task="当前要求")
        history = [
            HumanMessage("旧对话" * 1500),
            AIMessage("旧答复"),
            HumanMessage("另一个旧要求" * 300),
            AIMessage("旧答复"),
            HumanMessage("当前要求"),
        ]
        failed = AsyncMock(side_effect=ValueError("private-summary-failure"))
        with patch("workstation_agent.agent.context.SummarizationMiddleware.abefore_model", failed):
            await graph.ainvoke(
                {"messages": history},
                config={"configurable": {"thread_id": "test"}},
                context=runtime,
            )
        self.assertEqual(failed.await_count, 1)
        self.assertEqual(runtime.summary_calls, 1)
        self.assertEqual(len(model.requests), 2)
        self.assertTrue(
            all(usage["input_tokens"] <= settings.input_budget() for usage in runtime.usages)
        )
        self.assertIn("summary_unavailable_trim_fallback", runtime.usages[-1]["reasons"])
        self.assertNotIn("private-summary-failure", repr(runtime.usages))

    async def test_summary_cancellation_propagates_without_calling_primary_model(self) -> None:
        model = CapturingModel(responses=[AIMessage(content="不应调用")])
        graph = build_agent(
            model,
            [],
            context_settings=ContextSettings(window=16000, trigger_ratio=0.2),
            summary_model=ScriptedModel(responses=[AIMessage(content="摘要")]),
        )
        history = [
            HumanMessage("旧对话" * 1000),
            AIMessage("旧答复"),
            HumanMessage("第二轮"),
            AIMessage("旧答复"),
            HumanMessage("当前要求"),
        ]
        cancelled = AsyncMock(side_effect=asyncio.CancelledError())
        runtime = ContextRuntime(task="当前要求")
        with (
            patch(
                "workstation_agent.agent.context.SummarizationMiddleware.abefore_model", cancelled
            ),
            self.assertRaises(NodeCancelledError) as caught,
        ):
            await graph.ainvoke(
                {"messages": history},
                config={"configurable": {"thread_id": "test"}},
                context=runtime,
            )
        self.assertIsInstance(caught.exception.__cause__, asyncio.CancelledError)
        self.assertEqual(model.requests, [])

    async def test_final_rag_request_has_one_body_and_records_original_sources(self) -> None:
        evidence, context, _ = assemble_evidence([fragment("one", "独特原文正文")], 1000)
        from pydantic import TypeAdapter

        result = ToolResult(
            "search_documents",
            json.dumps({"evidence": evidence, "context": context}, ensure_ascii=False),
        )
        text = TypeAdapter(ToolResult).dump_json(result).decode()
        tool = StructuredTool.from_function(
            lambda query: text, name="search_documents", description="检索文档"
        )
        model = CapturingModel(
            responses=[
                call("search_documents", {"query": "第一次"}, "r1"),
                call("search_documents", {"query": "第二次"}, "r2"),
                AIMessage("完成"),
            ]
        )
        runtime = ContextRuntime(task="找文档")
        await build_agent(model, [tool]).ainvoke(
            {"messages": [HumanMessage("找文档")]},
            config={"configurable": {"thread_id": "test"}},
            context=runtime,
        )
        bodies = "\n".join(m.text for m in model.requests[-1] if isinstance(m, ToolMessage))
        self.assertEqual(bodies.count("独特原文正文"), 1)
        self.assertEqual(runtime.usages[-1]["sources"], ["rag:a.md:one@v"])

    async def test_explicit_memory_search_does_not_repeat_automatic_candidate_body(self) -> None:
        with TemporaryDirectory() as directory:
            memory = build_memory_application(Settings(Path(directory)), approve_write=True)
            record = memory.execute("remember_memory", {"content": "Python 使用唯一记忆正文"})
            model = CapturingModel(
                responses=[
                    call("search_memory", {"query": "Python"}, "search"),
                    AIMessage(content="完成"),
                ]
            )
            result = await AgentApplication(
                build_agent(model, local_tools(memory.tools)),
                context=ContextBuilder(memory=memory),
            ).run_task("查询 Python")
            self.assertEqual(result.status.value, "completed", result.error)
            contents = "\n".join(m.text for m in model.requests[-1])
            self.assertEqual(contents.count("Python 使用唯一记忆正文"), 1)
            last = json.loads(
                [s.output_summary for s in result.steps if s.name == "context_usage"][-1]
            )
            self.assertTrue(any(record["id"] in source for source in last["sources"]))

    async def test_memory_refresh_quota_drops_invalid_candidates(self) -> None:
        with TemporaryDirectory() as directory:
            settings = Settings(Path(directory))
            memory = build_memory_application(settings, approve_write=True)
            record = memory.execute("remember_memory", {"content": "Python 初始配置"})
            responses: list[BaseMessage] = [
                call(
                    "update_memory",
                    {
                        "memory_id": record["id"],
                        "expected_version": version,
                        "content": f"Python 配置版本 {version + 1}",
                    },
                    f"u{version}",
                )
                for version in range(1, 4)
            ]
            responses.append(AIMessage(content="完成"))
            model = CapturingModel(responses=responses)
            with patch.object(memory, "search", wraps=memory.search) as search:
                result = await AgentApplication(
                    build_agent(model, local_tools(memory.tools)),
                    context=ContextBuilder(memory=memory),
                ).run_task("更新 Python 配置")
            self.assertEqual(result.status.value, "completed", result.error)
            self.assertEqual(search.call_count, 3)
            usage = json.loads(
                [s.output_summary for s in result.steps if s.name == "context_usage"][-1]
            )
            self.assertEqual(usage["sources"], [])
            self.assertIn("memory_refresh_limit_invalidated", usage["reasons"])


class ContextBoundaryTests(unittest.TestCase):
    def test_unknown_capacity_and_invalid_output_configuration_are_rejected(self) -> None:
        model = ScriptedModel(responses=[AIMessage(content="")], profile=None)
        with self.assertRaises(ValueError):
            build_agent(model, [])
        for constructor in (
            lambda: ContextSettings(window=1000),
            lambda: ContextSettings(output_limit=10),
            lambda: ContextSettings(trigger_ratio=float("nan")),
        ):
            with self.assertRaises(ValueError):
                constructor()

    def test_structured_output_counts_each_official_union_binding(self) -> None:
        from langchain.agents.middleware.types import ModelRequest
        from langchain.agents.structured_output import OutputToolBinding, ToolStrategy
        from langchain_core.utils.function_calling import convert_to_openai_tool
        from langgraph.runtime import Runtime

        class FirstOutput(BaseModel):
            first: str = Field(description="第一种输出字段说明" * 100)

        class SecondOutput(BaseModel):
            second: str = Field(description="第二种输出字段说明" * 100)

        model = ScriptedModel(responses=[AIMessage(content="")])
        strategy = ToolStrategy(FirstOutput | SecondOutput)
        request = ModelRequest(
            model=model,
            messages=[],
            tools=[],
            state={"messages": []},
            runtime=Runtime(context=ContextRuntime()),
            response_format=strategy,
        )
        actual = [
            convert_to_openai_tool(OutputToolBinding.from_schema_spec(spec).tool)
            for spec in strategy.schema_specs
        ]
        self.assertGreaterEqual(
            RequestCounter(model).schemas(request),
            len(json.dumps(actual, ensure_ascii=False).encode("utf-8")),
        )

    def test_rag_body_is_sent_once_and_different_version_remains_distinct(self) -> None:
        evidence, context, _ = assemble_evidence([fragment("one", "唯一证据正文")], 1000)
        payload = json.dumps({"evidence": evidence, "context": context}, ensure_ascii=False)
        envelope = ToolResult("search_documents", payload)
        from pydantic import TypeAdapter

        text = TypeAdapter(ToolResult).dump_json(envelope).decode()
        messages = [
            ToolMessage(text, name="search_documents", tool_call_id="one"),
            ToolMessage(text, name="search_documents", tool_call_id="two"),
        ]
        selected = normalize(messages)
        self.assertEqual("\n".join(m.text for m in selected).count("唯一证据正文"), 1)
        self.assertIn("a.md:1-1@v", repr(selected))
        self.assertIn("context", text)

    def test_illegal_tool_pairs_are_rejected(self) -> None:
        for messages in (
            [ToolMessage("x", tool_call_id="orphan")],
            [call("read", {}, "missing"), HumanMessage("要求")],
        ):
            with self.assertRaises(ContextLimitExceeded):
                validate_pairs(messages)

    def test_board_protects_user_owned_fields_and_requires_source_for_facts(self) -> None:
        board = initialize([HumanMessage("用户目标", id="user")], None, ContextRuntime())
        forbidden_fields = {"goal": "篡改目标"}
        with self.assertRaises(ValueError):
            UpdateStatusArguments.model_validate(forbidden_fields)
        operation = UpdateTaskStatusTool(board, {"source": "实际工具结果"})
        facts = UpdateStatusArguments.model_validate(
            {"facts": [{"text": "虚构事实", "source": "source", "confirmed": True}]}
        )
        with self.assertRaises(ToolError):
            operation.run(facts)
        self.assertEqual(operation.board["goal"], "用户目标")
        unknown_board = json.loads(json.dumps({**board, "schema_version": 999}))
        with self.assertRaises(ValueError):
            restore_board(unknown_board)

    def test_memory_invalidation_also_removes_board_facts_and_completed_evidence(self) -> None:
        board = initialize([HumanMessage("任务", id="user")], None, ContextRuntime())
        board["facts"] = [{"text": "旧记忆", "source": "read", "confirmed": True}]
        board["items"] = [
            {"id": "task", "text": "检查", "status": "completed", "evidence": ["read"]}
        ]
        messages = [
            call("get_memory", {"memory_id": "memory"}, "read"),
            ToolMessage("旧记忆", tool_call_id="read", name="get_memory", id="result"),
        ]
        seen: set[str] = set()
        board = collect_results(board, messages, seen=seen)
        board = invalidate_memory_sources(board, {"read"})
        board = collect_results(board, messages, seen=seen)
        self.assertEqual(board["facts"], [])
        self.assertEqual(board["results"], [])
        self.assertEqual(board["items"][0]["status"], "blocked")

    def test_file_pages_reject_changed_source_and_do_not_escape_workspace(self) -> None:
        import re

        with TemporaryDirectory() as directory:
            path = Path(directory) / "note.txt"
            path.write_text("甲乙丙丁戊己", encoding="utf-8")
            tool = ReadTextFileTool(Path(directory))
            first = tool.invoke({"path": "note.txt", "limit": 3})
            match = re.search(r"version=([0-9-]+)", first)
            assert match is not None
            version = match.group(1)
            second = tool.invoke(
                {"path": "note.txt", "offset": 3, "limit": 3, "expected_version": version}
            )
            self.assertEqual(second, "丁戊己")
            path.write_text("修改后的文件", encoding="utf-8")
            # 两次内容的字节数相同，显式推进时间戳，避免依赖 Windows 的更新时间粒度。
            import os

            rewritten = path.stat()
            os.utime(path, ns=(rewritten.st_atime_ns, rewritten.st_mtime_ns + 1_000_000_000))
            with self.assertRaises(ToolError) as caught:
                tool.invoke({"path": "note.txt", "offset": 3, "expected_version": version})
            self.assertEqual(caught.exception.code, "source_changed")
            with self.assertRaises(ToolError):
                tool.invoke({"path": "../private.txt", "offset": 3})
