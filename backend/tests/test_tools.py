from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from workstation_agent.tools import ReadTextFileTool, ToolError


class ReadTextFileToolTests(unittest.TestCase):
    def test_reads_file_inside_workspace(self) -> None:
        with TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / "hello.txt").write_text("你好", encoding="utf-8")

            self.assertEqual(ReadTextFileTool(workspace).invoke({"path": "hello.txt"}), "你好")

    def test_rejects_path_outside_workspace(self) -> None:
        with TemporaryDirectory() as directory:
            tool = ReadTextFileTool(Path(directory))

            with self.assertRaises(ToolError):
                tool.invoke({"path": "../secret.txt"})


if __name__ == "__main__":
    unittest.main()
