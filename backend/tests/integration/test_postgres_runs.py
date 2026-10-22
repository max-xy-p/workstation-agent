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
