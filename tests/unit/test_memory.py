from __future__ import annotations

import unittest

from workstation_agent.memory.models import MemoryAccess, MemoryRecord, MemoryScope, MemorySource
from workstation_agent.memory.service import validate_content
from workstation_agent.tools.contracts import ToolError


class MemoryRulesTests(unittest.TestCase):
    def test_access_never_includes_another_project(self):
        access = MemoryAccess(user_id="local", workspace_id="ws", project_id="one")
        self.assertTrue(access.allows(access.scope("project")))
        self.assertFalse(
            access.allows(
                MemoryScope(user_id="local", workspace_id="ws", project_id="two", level="project")
            )
        )

    def test_sources_and_self_contained_text_are_required(self):
        scope = MemoryAccess(user_id="local", workspace_id="ws").scope("workspace")
        with self.assertRaises(ValueError):
            MemoryRecord(content=" ", scope=scope, sources=())
        with self.assertRaises(ValueError):
            MemoryRecord(content="使用中文", scope=scope, sources=())

    def test_secrets_are_rejected_without_echo(self):
        for text in ("api_key=private-value", "Bearer abcdefghijklmnop", "sk-" + "x" * 30):
            with self.subTest(), self.assertRaises(ToolError) as caught:
                validate_content(text)
            self.assertNotIn(text, str(caught.exception))

    def test_note_keeps_conditions_and_provenance(self):
        scope = MemoryAccess(user_id="local", workspace_id="ws").scope("workspace")
        record = MemoryRecord(
            content="仅 Windows 环境使用此排查步骤。",
            scope=scope,
            sources=(MemorySource(origin="explicit", run_id="run_1"),),
        )
        self.assertEqual(record.version, 1)
        self.assertEqual(record.sources[0].run_id, "run_1")
