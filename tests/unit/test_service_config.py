from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from workstation_agent.config import Settings


class ServiceConfigTests(unittest.TestCase):
    def test_defaults_and_secret_repr(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            settings = Settings.from_env()
        self.assertEqual(settings.service_url, "http://127.0.0.1:8765/mcp")
        self.assertIsNone(settings.service_token)
        secret = "private_test_credential_" + "x" * 32
        self.assertNotIn(secret, repr(Settings(Path.cwd(), service_token=secret)))

    def test_invalid_ports_are_rejected(self) -> None:
        for raw in ("0", "65536", "-1", "secret-not-a-port"):
            with (
                self.subTest(value=raw),
                patch.dict(
                    "os.environ",
                    {"WORKSTATION_AGENT_SERVICE_PORT": raw},
                    clear=True,
                ),
            ):
                with self.assertRaises(ValueError) as caught:
                    Settings.from_env()
                self.assertNotIn(raw, str(caught.exception))

    def test_token_validation_does_not_echo_credentials(self) -> None:
        for secret in ("too-short-private", "x" * 300, "x" * 32 + "\n", "x" * 32 + "非"):
            with (
                self.subTest(),
                patch.dict(
                    "os.environ",
                    {"WORKSTATION_AGENT_SERVICE_TOKEN": secret},
                    clear=True,
                ),
            ):
                with self.assertRaises(ValueError) as caught:
                    Settings.from_env()
                self.assertNotIn(secret, str(caught.exception))

    def test_direct_configuration_enforces_new_fields(self) -> None:
        workspace = Path.cwd()
        for port in (True, 0, 65536, "8765"):
            with self.subTest(port=port), self.assertRaises(ValueError):
                Settings(workspace, service_port=port)
