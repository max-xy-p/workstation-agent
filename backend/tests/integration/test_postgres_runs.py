from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from langchain_core.messages import AIMessage

from tests.test_agent import ScriptedModel
from workstation_agent.agent.factory import build_agent
from workstation_agent.application import AgentApplication
from workstation_agent.bootstrap import database_for
from workstation_agent.config import Settings
from workstation_agent.domain import Run, RunStatus, Step, StepKind
from workstation_agent.storage import PostgresRunRepository
from workstation_agent.api.conversations import PageRequest
from workstation_agent.tools.contracts import ToolError


class PostgresRunRepositoryTests(unittest.TestCase):
    def test_round_trips_run_and_steps(self) -> None:
        with TemporaryDirectory() as directory:
            repository = PostgresRunRepository(database_for(Settings(Path(directory))))
            run = Run(task="inspect project", session_id="session_1")
            run.transition(RunStatus.THINKING)
            step = Step(run_id=run.id, kind=StepKind.MODEL, name="complete")
            step.finish("done")
            run.steps.append(step)
            run.complete("done")

            repository.save(run)
            restored = repository.get(run.id)

            self.assertIsNotNone(restored)
            assert restored is not None
            self.assertEqual(restored.status, RunStatus.COMPLETED)
            self.assertEqual(restored.answer, "done")
            self.assertEqual(restored.steps[0].output_summary, "done")

    def test_scope_pagination_and_delete_are_isolated(self) -> None:
        with TemporaryDirectory() as directory:
            database = database_for(Settings(Path(directory)))
            first = PostgresRunRepository(database, scope="user-one")
            other = PostgresRunRepository(database, scope="user-two")
            run = Run(task="第一身份的记录", session_id="shared-session", id="same-run")
            run.complete("第一身份答复")
            first.save(run)
            self.assertIsNone(other.get(run.id))
            self.assertEqual(other.list_runs(PageRequest()).items, [])
            with self.assertRaises(ToolError):
                other.delete(run.id)
            second = Run(task="另一个身份", session_id="shared-session", id=run.id)
            other.save(second)
            with self.assertRaises(ToolError) as caught:
                other.delete(run.id)
            self.assertEqual(caught.exception.code, "session_busy")
            first.delete(run.id)
            self.assertIsNone(first.get(run.id))
            self.assertIsNotNone(other.get(run.id))

    def test_json_secrets_are_redacted_without_corrupting_payload(self) -> None:
        with TemporaryDirectory() as directory:
            secret = 'private-"quoted"-credential'
            repository = PostgresRunRepository(database_for(Settings(Path(directory))), secrets=(secret,))
            run = Run(task="password=do-not-store", session_id="redacted")
            run.task_status = {
                "schema_version": 2, "revision": 0, "task_id": "task", "session_id": run.session_id,
                "updated_at": run.updated_at.isoformat(), "goal": secret, "current_request": "检查脱敏",
                "criteria": [], "constraints": [], "items": [],
                "facts": [{"text": secret, "source": "用户输入", "confirmed": False}],
                "results": [], "blockers": [], "next_action": "",
            }
            run.complete("原文 " + secret)
            repository.save(run)
            stored = repository.get(run.id)
            assert stored is not None
            self.assertNotIn(secret, stored.answer or "")
            self.assertNotIn("do-not-store", stored.task)
            assert stored.task_status is not None
            self.assertNotIn(secret, stored.task_status["goal"])
            self.assertNotIn(secret, stored.task_status["facts"][0]["text"])
            self.assertEqual(run.answer, "原文 " + secret)

    def test_agent_persists_completed_run(self) -> None:
        with TemporaryDirectory() as directory:
            repository = PostgresRunRepository(database_for(Settings(Path(directory))))
            graph = build_agent(ScriptedModel(responses=[AIMessage(content="persisted answer")]), [])
            run = asyncio.run(AgentApplication(graph, repository=repository).run_task("save this run"))

            restored = repository.get(run.id)

            self.assertIsNotNone(restored)
            assert restored is not None
            self.assertEqual(restored.status, RunStatus.COMPLETED)
            self.assertEqual(restored.answer, "persisted answer")


if __name__ == "__main__":
    unittest.main()
