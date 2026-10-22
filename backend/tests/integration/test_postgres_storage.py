"""验证 PostgreSQL 共享实例下的工作区隔离、事务锁和官方迁移边界。"""

import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier

from workstation_agent.bootstrap import database_for
from workstation_agent.config import Settings
from workstation_agent.domain.runs import Run
from workstation_agent.memory.models import MemoryRecord, MemorySource
from workstation_agent.storage.postgres.checkpoints import PostgresSessionBackend
from workstation_agent.storage.postgres.memory import PostgresMemoryRepository
from workstation_agent.storage.postgres.runs import PostgresRunRepository
from workstation_agent.tools.contracts import ToolError


class PostgresStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = Settings(Path(self.temp.name))
        self.database = database_for(self.settings)

    def test_user_scope_memory_is_still_isolated_by_workspace(self) -> None:
        scope = self.settings.memory_access.scope("user")
        record = MemoryRecord(content="喜欢中文", scope=scope,
                              sources=(MemorySource(origin="explicit", run_id="one"),))
        repository = PostgresMemoryRepository(self.database)
        repository.apply((record,), {record.id: None}, scope)
        other = replace(self.settings, workspace=self.settings.workspace / "other")
        self.assertEqual(scope, other.memory_access.scope("user"))
        self.assertEqual(PostgresMemoryRepository(database_for(other)).records((scope,)), ())
        self.assertEqual(repository.records((scope,)), (record,))

    def test_concurrent_cross_scope_id_collision_preserves_record_owner(self) -> None:
        scopes = (self.settings.memory_access.scope("user"), self.settings.memory_access.scope())
        records = tuple(MemoryRecord(
            id="same-id", content="范围" + str(index), scope=scope,
            sources=(MemorySource(origin="explicit", run_id="one"),),
        ) for index, scope in enumerate(scopes))
        barrier = Barrier(2)

        def publish(record: MemoryRecord) -> bool:
            barrier.wait(timeout=5)
            try:
                PostgresMemoryRepository(self.database).apply((record,), {record.id: None}, record.scope)
                return True
            except ToolError as exc:
                self.assertEqual(exc.code, "version_conflict")
                return False

        with ThreadPoolExecutor(max_workers=2) as workers:
            self.assertEqual(sum(workers.map(publish, records)), 1)
        stored = PostgresMemoryRepository(self.database).records(scopes)
        self.assertEqual(len(stored), 1)
        self.assertIn(stored[0], records)

    def test_checkpoint_migrations_release_pooled_connection_lock(self) -> None:
        with ThreadPoolExecutor(max_workers=2) as workers:
            futures = [workers.submit(PostgresSessionBackend(self.database).initialize) for _ in range(2)]
            for future in futures:
                future.result(timeout=10)
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT pg_try_advisory_xact_lock(%s)",
                (self.database.lock_key("checkpoint-migrations", global_lock=True),),
            ).fetchone()
            assert row is not None
            self.assertTrue(row[0])

    def test_read_transactions_cannot_write_and_pool_resets_settings(self) -> None:
        self.database.initialize()
        with self.database.transaction() as connection:
            assert connection is not None
            self.assertEqual(connection.execute("SHOW transaction_read_only").fetchone(), ("on",))
        with self.database.connect() as connection:
            self.assertEqual(connection.execute("SHOW transaction_read_only").fetchone(), ("off",))
        with self.database.checkpoint_connection() as connection:
            connection.execute("SET default_transaction_read_only TO on")
            connection.execute("SET search_path TO workstation_checkpoints")
        with self.database.checkpoint_connection() as connection:
            self.assertEqual(connection.execute("SHOW default_transaction_read_only").fetchone(),
                             {"default_transaction_read_only": "off"})

    def test_run_storage_redacts_secrets_and_cannot_read_other_workspace(self) -> None:
        secret = "known-private-credential"
        repository = PostgresRunRepository(self.database, secrets=(secret,))
        run = Run(task="输入 " + secret, session_id="one")
        run.complete("答复 " + secret)
        repository.save(run)
        stored = repository.get(run.id)
        self.assertIsNotNone(stored)
        self.assertNotIn(secret, str(stored))
        self.assertIn(secret, run.task)
        other = database_for(replace(self.settings, workspace=self.settings.workspace / "other"))
        self.assertIsNone(PostgresRunRepository(other).get(run.id))
