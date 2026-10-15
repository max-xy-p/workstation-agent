"""验证策略可独立替换，且替换后仍受候选范围与原文边界约束。"""

import unittest
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from workstation_agent.application.rag import RagApplication
from workstation_agent.memory.models import MemoryAccess, MemoryRecord, MemoryScope, MemorySource
from workstation_agent.memory.service import MemoryService
from workstation_agent.rag.config import RagSettings
from workstation_agent.rag.index import IndexSnapshot
from workstation_agent.rag.repository import InMemoryIndexRepository
from workstation_agent.retrieval.strategy import RetrievalHit
from workstation_agent.tools.contracts import ToolError


class RecordingStrategy[T]:
    """记录服务交给策略的候选，并返回预先指定的排序结果。"""

    def __init__(self, hits: tuple[RetrievalHit, ...] = ()) -> None:
        self.hits = hits
        self.candidates: tuple[T, ...] = ()
        self.calls = 0
        self.limit = 0

    def retrieve(
        self, query: str, candidates: Sequence[T], *, limit: int
    ) -> tuple[RetrievalHit, ...]:
        self.calls += 1
        self.candidates = tuple(candidates)
        self.limit = limit
        return self.hits


class StaticMemoryRepository:
    """故意返回全部记录，验证服务自身也会检查范围和过期时间。"""

    def __init__(self, records: tuple[MemoryRecord, ...]) -> None:
        self.items = records

    def records(self, scopes: tuple[MemoryScope, ...]) -> tuple[MemoryRecord, ...]:
        return self.items

    def generation(self, scope: MemoryScope) -> int:
        raise AssertionError("检索不应读取写入状态")

    def processed(self, scope: MemoryScope, batch_id: str) -> bool:
        raise AssertionError("检索不应读取批次状态")

    def apply(
        self,
        records: tuple[MemoryRecord, ...],
        expected: dict[str, int | None],
        scope: MemoryScope,
        *,
        generation: int | None = None,
        batch_id: str | None = None,
    ) -> bool:
        raise AssertionError("检索不应修改记忆")

    def delete(self, memory_id: str, scope: MemoryScope, version: int) -> bool:
        raise AssertionError("检索不应删除记忆")


def note(access: MemoryAccess, content: str, *, expired: bool = False) -> MemoryRecord:
    return MemoryRecord(
        content=content,
        scope=access.scope(),
        sources=(MemorySource(origin="explicit", run_id="fixture"),),
        expires_at=datetime.now(UTC) - timedelta(days=1) if expired else None,
    )


class MemoryStrategyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.access = MemoryAccess(user_id="local", workspace_id="ws", project_id="one")
        self.current = note(self.access, "使用中文")
        self.expired = note(self.access, "旧偏好", expired=True)
        self.foreign = note(
            MemoryAccess(user_id="local", workspace_id="ws", project_id="two"), "其他项目"
        )
        self.repository = StaticMemoryRepository((self.current, self.expired, self.foreign))

    def test_custom_strategy_receives_only_active_owned_notes(self) -> None:
        strategy = RecordingStrategy[MemoryRecord]((RetrievalHit(self.current.id, 0.8),))
        service = MemoryService(self.repository, retrieval=strategy)
        # 无字面匹配也能返回结果，证明服务没有在调用策略前固定使用 BM25 过滤。
        self.assertEqual(service.search(self.access, "language", limit=2), (self.current,))
        self.assertEqual(strategy.candidates, (self.current,))
        self.assertEqual(strategy.limit, 2)

    def test_default_strategy_keeps_whole_note_and_excludes_unrelated_content(self) -> None:
        service = MemoryService(self.repository)
        self.assertEqual(service.search(self.access, "中文"), (self.current,))
        self.assertEqual(service.search(self.access, "天气"), ())

    def test_invalid_strategy_results_are_rejected(self) -> None:
        for hits in (
            (RetrievalHit(self.foreign.id, 1.0),),
            (RetrievalHit(self.expired.id, 1.0),),
            (RetrievalHit("unknown", 1.0),),
            (RetrievalHit(self.current.id, float("nan")),),
            (RetrievalHit(self.current.id, float("inf")),),
            (RetrievalHit(self.current.id, 1.0), RetrievalHit(self.current.id, 0.5)),
        ):
            with self.subTest(hits=hits), self.assertRaises(ToolError) as caught:
                service = MemoryService(self.repository, retrieval=RecordingStrategy(hits))
                service.search(self.access, "中文")
            self.assertEqual(caught.exception.code, "invalid_retrieval_output")

    def test_service_enforces_limit_even_if_strategy_returns_more(self) -> None:
        second = note(self.access, "另一个偏好")
        strategy = RecordingStrategy[MemoryRecord](
            (RetrievalHit(second.id, 0.9), RetrievalHit(self.current.id, 0.8))
        )
        service = MemoryService(
            StaticMemoryRepository((self.current, second)), retrieval=strategy
        )
        self.assertEqual(service.search(self.access, "任意问题", limit=1), (second,))

    def test_empty_candidates_skip_strategy(self) -> None:
        strategy = RecordingStrategy[MemoryRecord]()
        service = MemoryService(StaticMemoryRepository(()), retrieval=strategy)
        self.assertEqual(service.search(self.access, "中文"), ())
        self.assertEqual(strategy.calls, 0)


class KnowledgeStrategyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = InMemoryIndexRepository()
        self.settings = RagSettings(expansion=0)
        self.original = RagApplication(self.repository, self.settings)
        self.original.ingest("one.md", "# 设置\n使用中文交流。", {"project": "one"})
        self.original.ingest("two.md", "# 设置\n其他项目内容。", {"project": "two"})
        first = self.repository.get("one.md")
        second = self.repository.get("two.md")
        assert first is not None and second is not None
        self.first = first
        self.second = second

    def test_custom_strategy_gets_filtered_snapshots_and_original_citations(self) -> None:
        chunk = self.first.chunks[-1]
        assert chunk.id is not None
        strategy = RecordingStrategy[IndexSnapshot]((RetrievalHit(chunk.id, 0.7),))
        app = RagApplication(self.repository, self.settings, retrieval=strategy)
        result = app.search("language", {"project": "one"}, 1)
        self.assertEqual(strategy.candidates, (self.first,))
        self.assertEqual(result["used_chunk_ids"], [chunk.id])
        self.assertEqual(result["evidence"][0]["content"], chunk.page_content)
        self.assertEqual(result["evidence"][0]["score"], 0.7)
        self.assertIn("one.md:", result["evidence"][0]["citation"])
        self.assertNotIn("其他项目", result["context"])

    def test_outside_chunk_and_nonfinite_score_are_rejected(self) -> None:
        current_id = self.first.chunks[0].id
        foreign_id = self.second.chunks[0].id
        assert current_id is not None and foreign_id is not None
        for hits in (
            (RetrievalHit(foreign_id, 1.0),),
            (RetrievalHit("unknown", 1.0),),
            (RetrievalHit(current_id, float("nan")),),
            (RetrievalHit(current_id, 1.0), RetrievalHit(current_id, 0.5)),
        ):
            app = RagApplication(
                self.repository, self.settings, retrieval=RecordingStrategy(hits)
            )
            with self.subTest(hits=hits), self.assertRaises(ToolError) as caught:
                app.search("设置", {"project": "one"}, 1)
            self.assertEqual(caught.exception.code, "invalid_retrieval_output")

    def test_switching_strategy_does_not_require_rebuilding_index(self) -> None:
        app = RagApplication(
            self.repository, self.settings, retrieval=RecordingStrategy[IndexSnapshot]()
        )
        self.assertEqual(app.fingerprint, self.original.fingerprint)
        self.assertEqual(app.status()["status"], "ready")
        self.assertEqual(app.search("设置", {}, 1)["status"], "no_evidence")

    def test_empty_filtered_candidates_skip_strategy(self) -> None:
        strategy = RecordingStrategy[IndexSnapshot]()
        app = RagApplication(self.repository, self.settings, retrieval=strategy)
        self.assertEqual(app.search("设置", {"project": "missing"}, 1)["status"], "no_evidence")
        self.assertEqual(strategy.calls, 0)

    def test_memory_and_knowledge_keep_independent_tokenization(self) -> None:
        access = MemoryAccess(user_id="local", workspace_id="ws")
        record = note(access, "工作方式")
        memory = MemoryService(StaticMemoryRepository((record,)))
        self.original.ingest("work.txt", "工作方式", {})
        self.assertEqual(memory.search(access, "工"), ())
        result = self.original.search("工", {"source_uri": "work.txt"}, 1)
        self.assertEqual(result["status"], "ok")
