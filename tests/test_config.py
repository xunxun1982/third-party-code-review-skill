import importlib.util
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT_PATH = Path(__file__).parents[1] / "scripts" / "codereview_client.py"
SPEC = importlib.util.spec_from_file_location("codereview_client_config", SCRIPT_PATH)
CLIENT = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(CLIENT)


class ConfigTests(unittest.TestCase):
    def test_defaults_match_the_configured_review_service(self):
        config = CLIENT.Config({})

        self.assertEqual(
            config.base_url,
            "http://172.28.100.252:10130/proxy/codereview_chat",
        )
        self.assertEqual(config.protocol, "openai_chat")
        self.assertEqual(config.model, "codereview")
        self.assertFalse(hasattr(config, "max_input_chars"))
        self.assertEqual(config.timeout_seconds, 600)
        self.assertEqual(config.max_retries, 3)
        self.assertTrue(config.stream)

    def test_max_retries_accepts_zero_to_ten(self):
        self.assertEqual(CLIENT.Config({"max_retries": 0}).max_retries, 0)
        self.assertEqual(CLIENT.Config({"max_retries": 10}).max_retries, 10)
        self.assertEqual(CLIENT.Config({"max_retries": "3"}).max_retries, 3)

    def test_max_retries_rejects_invalid_values(self):
        for value in [-1, 11, True, "3.5", "invalid"]:
            with self.subTest(value=value):
                with self.assertRaisesRegex(CLIENT.ConfigError, "MAX_RETRIES"):
                    CLIENT.Config({"max_retries": value})

    def test_environment_configures_max_retries(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                "USERPROFILE": str(Path(tmp) / "missing-user"),
                "THIRD_PARTY_CODEREVIEW_MAX_RETRIES": "6",
            }
            with mock.patch.dict(os.environ, env, clear=True):
                values = CLIENT.load_config_values(Path(tmp) / "missing-skill")

        self.assertEqual(CLIENT.Config(values).max_retries, 6)

    def test_stream_defaults_to_true_and_accepts_boolean_values(self):
        self.assertTrue(CLIENT.Config({}).stream)
        self.assertTrue(CLIENT.Config({"stream": True}).stream)
        self.assertFalse(CLIENT.Config({"stream": False}).stream)
        self.assertTrue(CLIENT.Config({"stream": "true"}).stream)
        self.assertFalse(CLIENT.Config({"stream": "false"}).stream)
        with self.assertRaisesRegex(CLIENT.ConfigError, "STREAM"):
            CLIENT.Config({"stream": "auto"})

    def test_fixed_upstream_sections_select_the_first_two_enabled_entries(self):
        values = {
            "upstream_disabled": {"ENABLED": False},
            "upstream_codereview": {
                "ENABLED": True,
                "PROTOCOL": "openai_chat",
                "BASE_URL": "http://review-one.test/chat",
                "MODEL": "model-one",
                "API_KEY": "KEY_ONE",
                "TIMEOUT_SECONDS": 700,
                "MAX_RETRIES": 0,
            },
            "upstream2": {
                "ENABLED": True,
                "PROTOCOL": "anthropic",
                "BASE_URL": "https://review-two.test/messages",
                "MODEL": "model-two",
                "API_KEY": "KEY_TWO",
                "TIMEOUT_SECONDS": 800,
                "MAX_RETRIES": 3,
            },
            "upstream3": {
                "ENABLED": True,
            },
        }

        configs, ignored = CLIENT.select_upstream_configs(values)

        self.assertEqual(len(configs), 2)
        self.assertEqual(ignored, 1)
        self.assertEqual(configs[0].base_url, "http://review-one.test/chat")
        self.assertEqual(configs[0].protocol, "openai_chat")
        self.assertEqual(configs[0].model, "model-one")
        self.assertEqual(configs[0].timeout_seconds, 700)
        self.assertEqual(configs[0].max_retries, 0)
        self.assertEqual(configs[1].base_url, "https://review-two.test/messages")
        self.assertEqual(configs[1].protocol, "anthropic")
        self.assertEqual(configs[1].model, "model-two")
        self.assertEqual(configs[1].max_retries, 3)

    def test_config_file_accepts_named_upstream_tables(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text(
                '[UPSTREAM_codereview]\n'
                'ENABLED = true\n'
                'PROTOCOL = "openai_chat"\n'
                'BASE_URL = "http://review.test/review"\n'
                'MODEL = "codereview"\n'
                'API_KEY = ""\n',
                encoding="utf-8",
            )

            values = CLIENT.read_config_file(path)
            configs, ignored = CLIENT.select_upstream_configs(values)

        self.assertEqual(ignored, 0)
        self.assertEqual(len(configs), 1)
        self.assertEqual(configs[0].base_url, "http://review.test/review")
        self.assertEqual(configs[0].api_key, "")

    def test_config_file_accepts_utf8_bom_without_rewriting(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            original = b'\xef\xbb\xbfMODEL = "bom-model"\n'
            path.write_bytes(original)

            values = CLIENT.read_config_file(path)

            self.assertEqual(values["model"], "bom-model")
            self.assertEqual(path.read_bytes(), original)

    def test_user_config_is_preferred_without_mixing_environment_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            userprofile = Path(tmp) / "user"
            config_path = (
                userprofile
                / ".config"
                / "third-party-code-review-skill"
                / "config.toml"
            )
            config_path.parent.mkdir(parents=True)
            config_path.write_text(
                'BASE_URL = "http://config.test/review"\n'
                'MODEL = "configured-model"\n'
                'API_KEY = ""\n',
                encoding="utf-8",
            )

            env = {
                "USERPROFILE": str(userprofile),
                "THIRD_PARTY_CODEREVIEW_API_KEY": "ENV_SECRET",
                "THIRD_PARTY_CODEREVIEW_MODEL": "environment-model",
            }
            with mock.patch.dict(os.environ, env, clear=True):
                values = CLIENT.load_config_values(CLIENT.SKILL_ROOT)

        config = CLIENT.Config(values)
        self.assertEqual(config.base_url, "http://config.test/review")
        self.assertEqual(config.model, "configured-model")
        self.assertEqual(config.api_key, "")

    def test_load_config_state_reports_active_source_and_candidate_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            user_root = root / "user"
            user_config = (
                user_root
                / ".config"
                / CLIENT.APP_DIR_NAME
                / "config.toml"
            )
            user_config.parent.mkdir(parents=True)
            user_config.write_text(
                'PROTOCOL = "anthropic"\n'
                'BASE_URL = "https://example.test/messages"\n'
                'MODEL = "review-model"\n'
                'API_KEY = "TEST_SECRET"\n',
                encoding="utf-8",
            )
            skill_root = root / "skill"
            skill_root.mkdir()
            (skill_root / "config.toml").write_text(
                'MODEL = "skill-model"\n', encoding="utf-8"
            )
            explicit = root / "explicit.toml"
            explicit.write_text('MODEL = "explicit-model"\n', encoding="utf-8")

            env = {
                "USERPROFILE": str(user_root),
                CLIENT.CONFIG_ENV: str(explicit),
            }
            with mock.patch.dict(os.environ, env, clear=True):
                values, source = CLIENT.load_config_state(skill_root)
                files = CLIENT.config_file_statuses(skill_root)

        self.assertEqual(source, str(user_config))
        self.assertEqual(values["protocol"], "anthropic")
        self.assertEqual(
            [(item["kind"], item["path"], item["exists"]) for item in files],
            [
                ("user", str(user_config), True),
                ("skill_local", str(skill_root / "config.toml"), True),
                ("explicit", str(explicit), True),
            ],
        )

    def test_load_config_state_reports_environment_and_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill_root = Path(tmp) / "missing-skill"
            with mock.patch.dict(
                os.environ,
                {CLIENT.MODEL_ENV: "environment-model"},
                clear=True,
            ):
                values, source = CLIENT.load_config_state(skill_root)

            self.assertEqual(values, {"model": "environment-model"})
            self.assertEqual(source, "environment")

            with mock.patch.dict(os.environ, {}, clear=True):
                values, source = CLIENT.load_config_state(skill_root)

        self.assertEqual(values, {})
        self.assertEqual(source, "defaults")

    def test_explicit_config_is_a_lowest_priority_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            explicit = root / "explicit.toml"
            explicit.write_text('MODEL = "explicit-model"\n', encoding="utf-8")
            user_config = (
                root
                / "user"
                / ".config"
                / "third-party-code-review-skill"
                / "config.toml"
            )
            user_config.parent.mkdir(parents=True)
            user_config.write_text('MODEL = "user-model"\n', encoding="utf-8")

            env = {
                "USERPROFILE": str(root / "user"),
                "THIRD_PARTY_CODEREVIEW_CONFIG": str(explicit),
            }
            with mock.patch.dict(os.environ, env, clear=True):
                values = CLIENT.load_config_values(root / "skill")

        self.assertEqual(CLIENT.Config(values).model, "user-model")

    def test_home_config_is_checked_when_userprofile_is_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            config_path = (
                home
                / ".config"
                / "third-party-code-review-skill"
                / "config.toml"
            )
            config_path.parent.mkdir(parents=True)
            config_path.write_text('MODEL = "home-model"\n', encoding="utf-8")

            with mock.patch.dict(os.environ, {"HOME": str(home)}, clear=True):
                values = CLIENT.load_config_values(Path(tmp) / "skill")

        self.assertEqual(CLIENT.Config(values).model, "home-model")

    def test_environment_uses_the_configured_base_url(self):
        env = {
            "THIRD_PARTY_CODEREVIEW_BASE_URL": "https://gateway.example/direct-review/",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            values = CLIENT.load_config_values(Path("missing-skill-root"))

        config = CLIENT.Config(values)
        self.assertEqual(config.base_url, "https://gateway.example/direct-review/")

    def test_explicit_config_is_used_when_higher_priority_sources_are_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            explicit = root / "explicit.toml"
            explicit.write_text('MODEL = "explicit-model"\n', encoding="utf-8")

            env = {
                "USERPROFILE": str(root / "missing-user"),
                "THIRD_PARTY_CODEREVIEW_CONFIG": str(explicit),
            }
            with mock.patch.dict(os.environ, env, clear=True):
                values = CLIENT.load_config_values(root / "skill")

        self.assertEqual(CLIENT.Config(values).model, "explicit-model")

    def test_invalid_toml_is_reported_instead_of_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "broken.toml"
            path.write_text('MODEL = "unterminated\n', encoding="utf-8")

            with mock.patch.dict(
                os.environ,
                {"THIRD_PARTY_CODEREVIEW_CONFIG": str(path)},
                clear=True,
            ):
                with self.assertRaisesRegex(CLIENT.ConfigError, "Failed to parse config file"):
                    CLIENT.load_config_values(Path(tmp) / "skill")

    def test_main_reports_config_errors_without_traceback(self):
        stderr = io.StringIO()
        with (
            mock.patch.object(
                CLIENT,
                "load_config_state",
                side_effect=CLIENT.ConfigError("broken config"),
            ),
            mock.patch("sys.stderr", stderr),
        ):
            exit_code = CLIENT.main(["doctor"])

        self.assertEqual(exit_code, 2)
        self.assertIn("Error: broken config", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())

    def test_anthropic_replaces_claude_messages(self):
        config = CLIENT.Config(
            {
                "protocol": "anthropic",
                "base_url": "https://gateway.example.test/messages",
                "model": "review-model",
                "api_key": "TEST_SECRET",
            }
        )
        self.assertEqual(config.protocol, "anthropic")

        with self.assertRaisesRegex(CLIENT.ConfigError, "PROTOCOL must be one of"):
            CLIENT.Config(
                {
                    "protocol": "claude_messages",
                    "base_url": "https://gateway.example.test/messages",
                    "model": "review-model",
                    "api_key": "TEST_SECRET",
                }
            )

    def test_invalid_scalar_types_and_ranges_are_rejected(self):
        invalid_cases = [
            ({"timeout_seconds": -1}, "TIMEOUT_SECONDS"),
            ({"base_url": "ftp://example.test"}, "BASE_URL"),
            ({"base_url": "https://user:pass@example.test"}, "BASE_URL"),
            ({"base_url": "https://example.test:70000"}, "BASE_URL"),
            ({"model": "   "}, "MODEL"),
            ({"protocol": "unknown"}, "PROTOCOL"),
            ({"stream": "sometimes"}, "STREAM"),
        ]

        for values, message in invalid_cases:
            with self.subTest(values=values):
                with self.assertRaisesRegex(CLIENT.ConfigError, message):
                    CLIENT.Config(values)

    def test_base_url_preserves_root_prefix_and_query(self):
        config = CLIENT.Config(
            {"base_url": "https://gateway.example/v1/responses?tenant=x"}
        )
        self.assertEqual(
            config.base_url,
            "https://gateway.example/v1/responses?tenant=x",
        )

    def test_base_url_rejects_unsafe_components(self):
        invalid_cases = [
            {"base_url": "https://gateway.example/root#fragment"},
        ]

        for values in invalid_cases:
            with self.subTest(values=values):
                with self.assertRaisesRegex(CLIENT.ConfigError, "BASE_URL"):
                    CLIENT.Config(values)

    def test_removed_request_policy_keys_are_rejected(self):
        for key in [
            "api_path",
            "context_window_tokens",
            "max_output_tokens",
            "anthropic_version",
            "max_input_chars",
        ]:
            with self.subTest(key=key):
                with self.assertRaisesRegex(CLIENT.ConfigError, key.upper()):
                    CLIENT.Config({key: "removed"})


if __name__ == "__main__":
    unittest.main()
