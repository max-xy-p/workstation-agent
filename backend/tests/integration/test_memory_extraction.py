"""离线验证真实 LangMem 协议、授权提交和自动提取失败边界。"""

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from langchain_core.messages import AIMessage

from tests.postgres_support import has_memory
from tests.test_agent import ScriptedModel
from workstation_agent.agent.factory import build_agent
from workstation_agent.application import AgentApplication, ToolApplication
from workstation_agent.bootstrap import MemoryApprovalPolicy, build_memory_application, database_for
from workstation_agent.config import Settings
from workstation_agent.memory.config import MemorySettings
from workstation_agent.memory.models import MemoryCandidate
from workstation_agent.memory.service import MemoryService
from workstation_agent.models.providers.memory import LangMemExtractor
from workstation_agent.storage.postgres.memory import PostgresMemoryRepository
from workstation_agent.tools import ToolExecutor, ToolRegistry
from workstation_agent.tools.contracts import ToolError
from workstation_agent.tools.extraction import MemoryExtractionTool


class FixedExtractor:
    """仅替代模型边界，不替代事务或授权流程。"""

    def __init__(self, candidates):
        self.candidates = candidates
        self.called = 0

    def extract(self, text, existing):
        self.called += 1
        return tuple(self.candidates)


class ExtractionIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.settings = Settings(self.root, memory=MemorySettings(auto_extract=True))
        self.repository = PostgresMemoryRepository(database_for(self.settings))
        self.service = MemoryService(self.repository)

    def runner(self, extractor, *, remote=True, automatic=True):
        policy = MemoryApprovalPolicy(remote=remote, automatic=automatic)
        return ToolApplication(
            ToolExecutor(
                ToolRegistry(
                    (
                        MemoryExtractionTool(
                            self.service,
                            self.settings.memory_access,
                            extractor,
                            self.settings.memory,
                            policy,
                        ),
                    )
                ),
                policy,
            )
        )

    def args(self, run_id="run_one"):
        return {"text": "我喜欢中文", "run_id": run_id, "session_id": "chat"}

    def candidate(self, **kwargs):
        return MemoryCandidate(
            content="用户喜欢中文", evidence="我喜欢中文", certain=True, **kwargs
        )

    async def test_real_langmem_protocol_and_idempotence(self):
        model = ScriptedModel(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "MemoryCandidate",
                            "args": self.candidate().model_dump(),
                            "id": "extract_one",
                            "type": "tool_call",
                        }
                    ],
                )
            ]
        )
        extractor = LangMemExtractor(model, MemoryApprovalPolicy(remote=True))
        runner = self.runner(extractor)
        result = runner.execute_tool("extract_memory", self.args())
        self.assertTrue(result.success, result)
        self.assertEqual(json.loads(result.content)["saved"], 1)
        again = runner.execute_tool("extract_memory", self.args())
        self.assertEqual(json.loads(again.content)["status"], "already_processed")
        note = self.service.records(self.settings.memory_access)[0]
        self.assertEqual(note.content, "用户喜欢中文")
        self.assertEqual(note.sources[0].origin, "extracted")
        self.service.forget(self.settings.memory_access, note.id, 1)
        self.assertEqual(
            json.loads(runner.execute_tool("extract_memory", self.args()).content)["status"],
            "already_processed",
        )

    async def test_denial_never_calls_model_or_creates_storage(self):
        extractor = FixedExtractor([self.candidate()])
        result = self.runner(extractor, remote=False).execute_tool("extract_memory", self.args())
        self.assertEqual(result.error_code, "permission_required")
        self.assertEqual(extractor.called, 0)
        self.assertFalse(has_memory(self.settings))

    async def test_candidate_cannot_bypass_write_permission(self):
        result = self.runner(FixedExtractor([self.candidate()]), automatic=False).execute_tool(
            "extract_memory", self.args()
        )
        self.assertEqual(result.error_code, "permission_required")
        self.assertFalse(has_memory(self.settings))

    async def test_uncertain_or_unsupported_claims_do_not_become_notes(self):
        candidates = [
            MemoryCandidate(content="喜欢英文", evidence="我喜欢英文", certain=True),
            MemoryCandidate(content="喜欢中文", evidence="中文", certain=False),
        ]
        result = self.runner(FixedExtractor(candidates)).execute_tool("extract_memory", self.args())
        self.assertTrue(result.success)
        self.assertEqual(self.service.records(self.settings.memory_access), ())
        self.assertEqual(json.loads(result.content)["pending"], 2)

    async def test_automatic_update_never_overwrites_explicit_memory(self):
        app = build_memory_application(self.settings, approve_write=True)
        old = app.execute("remember_memory", {"content": "用户喜欢中文"})
        candidate = MemoryCandidate(
            content="用户不喜欢中文",
            evidence="喜欢中文",
            certain=True,
            target_id=old["id"],
            expected_version=1,
        )
        result = self.runner(FixedExtractor([candidate])).execute_tool(
            "extract_memory", self.args()
        )
        self.assertTrue(result.success)
        self.assertEqual(app.execute("get_memory", {"memory_id": old["id"]})["version"], 1)

    async def test_forgotten_during_extraction_cannot_be_recreated(self):
        app = build_memory_application(self.settings, approve_write=True, approve_delete=True)
        old = app.execute("remember_memory", {"content": "用户喜欢中文"})
        candidate = self.candidate()

        class DeletingExtractor:
            """提取与删除交错，验证提交时再次检查水位。"""

            def extract(self, text, existing):
                app.execute("delete_memory", {"memory_id": old["id"], "expected_version": 1})
                return (candidate,)

        result = self.runner(DeletingExtractor()).execute_tool("extract_memory", self.args())
        self.assertEqual(result.error_code, "stale_extraction")
        self.assertEqual(app.execute("list_memories", {})["total"], 0)

    async def test_extraction_failure_does_not_change_completed_run(self):
        class BrokenExtractor:
            """供应商异常可能包含凭据，不能回显。"""

            def extract(self, text, existing):
                raise RuntimeError("private-provider-secret")

        runner = self.runner(BrokenExtractor())
        graph = build_agent(ScriptedModel(responses=[AIMessage(content="完成")]), [])
        run = await AgentApplication(graph, extraction=runner).run_task("我喜欢中文")
        self.assertEqual(run.status.value, "completed")
        self.assertEqual(run.answer, "完成")
        self.assertEqual(run.steps[-1].status.value, "failed")
        self.assertNotIn("private-provider-secret", repr(run))

    async def test_model_repair_cannot_exceed_outbound_call_budget(self):
        from workstation_agent.models.providers.memory import AuthorizedMemoryModel

        model = AuthorizedMemoryModel(
            delegate=ScriptedModel(responses=[AIMessage(content="ok")]),
            policy=MemoryApprovalPolicy(remote=True),
            max_calls=1,
        )
        model.invoke("one")
        bound_model = model.bind_tools([])
        with self.assertRaises(ToolError):
            bound_model.invoke("two")

    async def test_unchanged_langmem_snapshot_does_not_consume_candidate_budget(self):
        from workstation_agent.memory.models import MemoryRecord, MemorySource

        existing = tuple(
            MemoryRecord(
                content=f"已有偏好编号{i}",
                scope=self.settings.memory_access.scope(),
                sources=(MemorySource(origin="extracted", run_id="old"),),
            )
            for i in range(6)
        )
        model = ScriptedModel(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "MemoryCandidate",
                            "args": self.candidate().model_dump(),
                            "id": "new",
                            "type": "tool_call",
                        }
                    ],
                )
            ]
        )
        result = LangMemExtractor(model, MemoryApprovalPolicy(remote=True)).extract(
            "我喜欢中文",
            existing,
        )
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].content, "用户喜欢中文")
