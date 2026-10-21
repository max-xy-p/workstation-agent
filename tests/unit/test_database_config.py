"""验证数据库配置和安全错误，不访问 PostgreSQL 服务。"""

import unittest
from pathlib import Path
from unittest.mock import patch

from workstation_agent.config import Settings
from workstation_agent.storage.postgres.database import PostgresDatabase
from workstation_agent.tools.contracts import ToolError


class DatabaseConfigTests(unittest.TestCase):
    def test_url_is_loaded_but_hidden_in_configuration_repr(self) -> None:
        url = "postgresql://user:private-password@localhost/database"
        with patch.dict("os.environ", {"WORKSTATION_AGENT_DATABASE_URL": url}, clear=True):
            settings = Settings.from_env()
        self.assertEqual(settings.database_url, url)
        self.assertNotIn("private-password", repr(settings))

    def test_invalid_urls_do_not_echo_credentials(self) -> None:
        for url in ("sqlite:///local.db", "https://private-password@example.com/db",
                    "postgresql://user:private-password@localhost", "postgresql://localhost/db#private"):
            with self.subTest(), self.assertRaises(ValueError) as caught:
                Settings(Path.cwd(), database_url=url)
            self.assertNotIn("private-password", str(caught.exception))

    def test_missing_url_is_reported_without_connecting(self) -> None:
        database = PostgresDatabase(None, "test")
        with self.assertRaises(ToolError) as caught:
            database.check()
        self.assertEqual(caught.exception.code, "database_not_configured")
