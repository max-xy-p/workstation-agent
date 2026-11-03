"""验证笔记摄取绑定只来自可信配置，不接受模型覆盖来源。"""

import json
import unittest
from pathlib import Path
from unittest.mock import patch

from workstation_agent.config import Settings
from workstation_agent.integrations.workstation.config import WorkstationNotesConfig, workstation_notes_from_env
from workstation_agent.tools.mcp_config import MCPServerConfig


class WorkstationConfigTests(unittest.TestCase):
    def test_disabled_by_default_and_explicit_read_binding(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            self.assertIsNone(workstation_notes_from_env())
        server = MCPServerConfig(name="notes", url="http://127.0.0.1:8000/mcp", tools={"get_note": "read"})
        binding = WorkstationNotesConfig(server="notes", workspace_id=1)
        self.assertEqual(Settings(Path.cwd(), external_mcp_servers=(server,), workstation_notes=binding).workstation_notes, binding)
        for servers in ((), (server.model_copy(update={"tools": {"get_note": "write"}}),)):
            with self.assertRaises(ValueError):
                Settings(Path.cwd(), external_mcp_servers=servers, workstation_notes=binding)

    def test_invalid_configuration_does_not_echo_input(self) -> None:
        for data in ({"server": "private-secret", "workspace_id": 0}, {"server": "notes", "workspace_id": True},
                     {"server": "notes", "workspace_id": "1"}, {"server": "notes", "workspace_id": 1, "token": "private-secret"}):
            with self.subTest(data=data), patch.dict("os.environ", {"WORKSTATION_AGENT_WORKSTATION_NOTES": json.dumps(data)}):
                with self.assertRaises(ValueError) as caught:
                    workstation_notes_from_env()
                self.assertNotIn("private-secret", str(caught.exception))
