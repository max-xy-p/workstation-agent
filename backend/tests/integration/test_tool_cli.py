from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


class ToolCLIIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.workspace = Path(self.directory.name)
        (self.workspace / "hello.txt").write_text("Hello tools", encoding="utf-8")
        (self.workspace / "folder").mkdir()
        self.environment = os.environ.copy()
        self.environment.update({
            "WORKSTATION_AGENT_WORKSPACE": str(self.workspace),
            "WORKSTATION_AGENT_TOOL_OUTPUT_CHARS": "50000",
            "WORKSTATION_AGENT_MAX_STEPS": "8",
            "PYTHONIOENCODING": "utf-8",
        })

    def cli(self, *arguments: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "workstation_agent", *arguments],
            env=self.environment, capture_output=True, text=True, encoding="utf-8", timeout=20,
        )

    def test_lists_real_directory_with_default_path(self) -> None:
        process = self.cli("tool", "list_files")
        self.assertEqual(process.returncode, 0, process.stderr)
        response = json.loads(process.stdout)
        self.assertTrue(response["success"])
        self.assertEqual(json.loads(response["content"]), [
            {"name": "folder", "type": "directory"}, {"name": "hello.txt", "type": "file"},
        ])
        self.assertFalse((self.workspace / ".workstation-agent").exists())

    def test_reads_utf8_file_through_actual_entry(self) -> None:
        text = "Chinese text: " + chr(0x4f60) + chr(0x597d)
        (self.workspace / "hello.txt").write_text(text, encoding="utf-8")
        process = self.cli("tool", "read_text_file", "--path", "hello.txt")
        self.assertEqual(process.returncode, 0, process.stderr)
        response = json.loads(process.stdout)
        self.assertEqual(response["content"], text)
        self.assertIsNone(response["error_code"])

    def test_failure_codes_and_json_are_available_without_tracebacks(self) -> None:
        cases = [
            (("tool", "missing"), 2, "unknown_tool"),
            (("tool", "read_text_file"), 2, "invalid_arguments"),
            (("tool", "list_files", "--arguments", '{"path":123}'), 2, "invalid_arguments"),
            (("tool", "list_files", "--arguments", '{"extra":"secret"}'), 2, "invalid_arguments"),
            (("tool", "list_files", "--arguments", "[]"), 2, "invalid_arguments"),
            (("tool", "list_files", "--arguments", "secret-not-json"), 2, "invalid_json"),
            (("tool", "read_text_file", "--path", "../secret.txt"), 1, "path_outside_workspace"),
            (("tool", "read_text_file", "--path", str(self.workspace.parent / "secret.txt")),
             1, "path_outside_workspace"),
            (("tool", "read_text_file", "--path", "missing.txt"), 1, "not_found"),
        ]
        for arguments, expected_exit, expected_code in cases:
            with self.subTest(arguments=arguments):
                process = self.cli(*arguments)
                self.assertEqual(process.returncode, expected_exit, process.stderr)
                response = json.loads(process.stdout)
                self.assertFalse(response["success"])
                self.assertEqual(response["error_code"], expected_code)
                self.assertEqual(process.stderr, "")
                self.assertNotIn("secret-not-json", process.stdout)

    def test_trace_reports_actual_graph_nodes_without_file_content(self) -> None:
        process = self.cli("tool", "read_text_file", "--path", "hello.txt", "--trace")
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(json.loads(process.stdout)["content"], "Hello tools")
        self.assertEqual(process.stderr.splitlines(), [
            "langgraph: validate_request", "langgraph: execute_tool",
        ])
        self.assertNotIn("Hello tools", process.stderr)

    def test_rejects_invalid_utf8(self) -> None:
        (self.workspace / "binary.dat").write_bytes(bytes([255, 254, 128]))
        process = self.cli("tool", "read_text_file", "--path", "binary.dat")
        self.assertEqual(process.returncode, 1)
        self.assertEqual(json.loads(process.stdout)["error_code"], "invalid_utf8")

    def test_large_file_is_bounded_and_marked(self) -> None:
        self.environment["WORKSTATION_AGENT_TOOL_OUTPUT_CHARS"] = "32"
        (self.workspace / "large.txt").write_text("x" * 100000, encoding="utf-8")
        process = self.cli("tool", "read_text_file", "--path", "large.txt")
        self.assertEqual(process.returncode, 0)
        content = json.loads(process.stdout)["content"]
        self.assertLessEqual(len(content), 32)
        self.assertTrue(content.endswith("[truncated]"))

    def test_discovery_exposes_schema_and_permission(self) -> None:
        process = self.cli("tools", "--json")
        self.assertEqual(process.returncode, 0)
        catalog = {item["name"]: item for item in json.loads(process.stdout)}
        self.assertEqual(catalog["read_text_file"]["parameters"]["required"], ["path"])
        self.assertFalse(catalog["list_files"]["parameters"]["additionalProperties"])
        self.assertEqual(catalog["list_files"]["permission"], "SAFE_READ")

    def test_doctor_reports_validation_and_output_configuration(self) -> None:
        process = self.cli("doctor")
        self.assertEqual(process.returncode, 0)
        self.assertIn("pydantic:", process.stdout)
        self.assertIn("tool output chars: 50000", process.stdout)
        for invalid in ("zero", "0", "-1"):
            with self.subTest(value=invalid):
                self.environment["WORKSTATION_AGENT_TOOL_OUTPUT_CHARS"] = invalid
                process = self.cli("doctor")
                self.assertEqual(process.returncode, 2)
                self.assertNotIn("Traceback", process.stderr)

    def test_symlink_outside_workspace_cannot_be_read(self) -> None:
        with TemporaryDirectory() as outside:
            target = Path(outside) / "secret.txt"
            target.write_text("private-file-content", encoding="utf-8")
            link = self.workspace / "link.txt"
            try:
                link.symlink_to(target)
            except OSError:
                self.skipTest("this Windows account cannot create symbolic links")
            process = self.cli("tool", "read_text_file", "--path", "link.txt")
            self.assertEqual(process.returncode, 1)
            self.assertEqual(json.loads(process.stdout)["error_code"], "path_outside_workspace")
            self.assertNotIn("private-file-content", process.stdout)


if __name__ == "__main__":
    unittest.main()
