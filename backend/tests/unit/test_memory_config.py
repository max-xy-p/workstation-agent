"""记忆开关、预算和诊断配置的纯逻辑测试。"""

import unittest
from unittest.mock import patch

from workstation_agent.memory.config import MemorySettings


class MemoryConfigTests(unittest.TestCase):
    def test_defaults_and_environment(self):
        with patch.dict("os.environ", {}, clear=True):
            value = MemorySettings.from_env()
        self.assertTrue(value.enabled)
        self.assertFalse(value.auto_extract)
        self.assertFalse(value.sessions)
        with patch.dict(
            "os.environ",
            {
                "WORKSTATION_AGENT_MEMORY_AUTO_EXTRACT": "true",
                "WORKSTATION_AGENT_MEMORY_ALLOW_REMOTE": "1",
                "WORKSTATION_AGENT_MEMORY_EXTRACT_CALLS": "2",
                "WORKSTATION_AGENT_PROJECT_ID": "project",
            },
            clear=True,
        ):
            value = MemorySettings.from_env()
        self.assertTrue(value.auto_extract)
        self.assertTrue(value.allow_remote)
        self.assertEqual(value.extract_calls, 2)
        self.assertEqual(value.project_id, "project")

    def test_invalid_configuration_does_not_echo_raw_values(self):
        for name, raw in (
            ("MEMORY_ENABLED", "private-invalid"),
            ("MEMORY_EXTRACT_CALLS", "20"),
            ("MEMORY_SEARCH_LIMIT", "-1"),
        ):
            with (
                patch.dict("os.environ", {"WORKSTATION_AGENT_" + name: raw}, clear=True),
                self.assertRaises(ValueError) as caught,
            ):
                MemorySettings.from_env()
            self.assertNotIn(raw, str(caught.exception))
        with self.assertRaises(ValueError):
            MemorySettings(enabled=False, auto_extract=True)
