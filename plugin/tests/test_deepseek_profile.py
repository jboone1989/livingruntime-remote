"""Provider-profile routing regression tests."""
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from livingruntime_adapter_claude import validate_options
from claude_worker import cli_args, provider_environment


class ProviderProfileTests(unittest.TestCase):
    def test_profile_validation(self):
        self.assertEqual(validate_options({"provider_profile": "deepseek-flash"})["provider_profile"], "deepseek-flash")
        self.assertEqual(validate_options({"provider_profile": "claude-subscription"})["provider_profile"], "claude-subscription")
        for value in ({"provider_profile": "unknown"},
                      {"provider_profile": "deepseek-flash", "model": "opus"},
                      {"provider_profile": "deepseek-flash", "fallback_model": "sonnet"}):
            with self.assertRaises(ValueError):
                validate_options(value)

    def test_no_cli_model_flag_for_deepseek(self):
        record = {"executable": "/usr/bin/claude", "prompt": "test",
                  "session_id": "00000000-0000-4000-8000-000000000001",
                  "options": {"provider_profile": "deepseek-flash", "model": "deepseek-flash"}}
        self.assertNotIn("--model", cli_args(record))

    def test_subscription_has_no_external_routing(self):
        with patch.dict(os.environ, {}, clear=True):
            env = provider_environment({"options": {"provider_profile": "claude-subscription"}})
        self.assertNotIn("ANTHROPIC_BASE_URL", env)

    def test_deepseek_environment_does_not_inherit_other_provider(self):
        mock_env = {"DEEPSEEK_API_KEY": "unit-test-placeholder", "ANTHROPIC_BASE_URL": "https://not-used.invalid"}
        with patch.dict(os.environ, mock_env, clear=True):
            env = provider_environment({"options": {"provider_profile": "deepseek-flash"}})
        self.assertEqual(env["ANTHROPIC_BASE_URL"], "https://api.deepseek.com/anthropic")
        self.assertEqual(env["ANTHROPIC_MODEL"], "deepseek-flash[1m]")
        self.assertNotIn("DEEPSEEK_API_KEY", env)
        self.assertTrue(env["ANTHROPIC_AUTH_TOKEN"])

    def test_deepseek_requires_credentials(self):
        with patch.dict(os.environ, {}, clear=True), patch("claude_worker.Path.home", return_value=Path("/nonexistent")):
            with self.assertRaises(FileNotFoundError):
                provider_environment({"options": {"provider_profile": "deepseek-flash"}})
