"""RAG 配置边界与敏感值不回显。"""

import unittest
from pathlib import Path
from unittest.mock import patch

from workstation_agent.config import Settings
from workstation_agent.rag.config import RagSettings


class RagConfigTests(unittest.TestCase):
    def test_environment_and_invalid_values(self):
        with patch.dict(
            "os.environ",
            {
                "WORKSTATION_AGENT_RAG_EMBEDDING_MODEL": "model",
                "WORKSTATION_AGENT_RAG_ALLOW_REMOTE": "true",
                "WORKSTATION_AGENT_RAG_CHUNK_SIZE": "600",
            },
            clear=True,
        ):
            settings = Settings.from_env()
        self.assertEqual(settings.rag.chunk_size, 600)
        self.assertEqual(settings.rag.embedding_model, "model")
        self.assertTrue(settings.rag.allow_remote)
        for key, value in (
            ("ALLOW_REMOTE", "private-secret"),
            ("CHUNK_SIZE", "private-secret"),
            ("CHUNK_OVERLAP", "-1"),
            ("SEARCH_LIMIT", "21"),
        ):
            with (
                self.subTest(key=key),
                patch.dict(
                    "os.environ",
                    {
                        "WORKSTATION_AGENT_RAG_" + key: value,
                    },
                    clear=True,
                ),
            ):
                with self.assertRaises(ValueError) as caught:
                    Settings.from_env()
                self.assertNotIn("private-secret", str(caught.exception))

    def test_direct_budget_and_workspace_boundaries(self):
        for values in (
            {"chunk_size": True},
            {"chunk_overlap": True},
            {"expansion": -1},
            {"input_budget": 0},
            {"search_limit": 21},
        ):
            with self.subTest(values=values), self.assertRaises(ValueError):
                RagSettings(**values)
        settings = Settings(Path.cwd(), data_dir=Path.cwd().parent / "outside")
        with self.assertRaises(ValueError):
            _ = settings.rag_database_path
