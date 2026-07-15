import re
import tomllib
import unittest
from pathlib import Path


SKILL_ROOT = Path(__file__).parents[1]
SKILL_MD = SKILL_ROOT / "SKILL.md"
README = SKILL_ROOT / "README.md"
OPENAI_YAML = SKILL_ROOT / "agents" / "openai.yaml"
CONFIG_EXAMPLE = SKILL_ROOT / "config.example.toml"
CONFIG_REFERENCE = SKILL_ROOT / "references" / "configuration.md"
GITIGNORE = SKILL_ROOT / ".gitignore"


class SkillContractTests(unittest.TestCase):
    def assert_client_simulation_documented(self, text):
        for phrase in [
            "SIMULATED_CLIENT",
            "disabled by default",
            "openai_chat does not currently support client simulation",
            "streaming and non-streaming requests",
        ]:
            self.assertIn(phrase, text)
        for protocol, profile in [
            ("openai_chat", "Unsupported"),
            ("openai_responses", "Codex CLI 0.144.4"),
            ("anthropic", "Claude Code 2.1.210"),
        ]:
            self.assertRegex(
                text,
                rf"(?m)^\| `{protocol}` \|[^\n]*{re.escape(profile)}[^\n]*\|$",
            )

    def test_skill_repository_contains_no_han_characters(self):
        text_files = [
            path
            for path in SKILL_ROOT.rglob("*")
            if path.is_file()
            and ".git" not in path.parts
            and "__pycache__" not in path.parts
            and "docs" not in path.parts
            and path.name != "implementation-notes.md"
            and path.name != "LICENSE"
        ]

        for path in text_files:
            if path.suffix not in {".md", ".py", ".toml", ".yaml", ".yml"} and path.name != ".gitignore":
                continue
            text = path.read_text(encoding="utf-8")
            with self.subTest(path=path.relative_to(SKILL_ROOT)):
                self.assertIsNone(re.search(r"[\u3400-\u9fff]", text))

    def test_skill_metadata_and_body_are_complete(self):
        text = SKILL_MD.read_text(encoding="utf-8")

        self.assertNotIn("TODO", text)
        self.assertRegex(text, r"(?m)^name: third-party-code-review-skill$")
        description = re.search(r"(?m)^description: (.+)$", text)
        self.assertIsNotNone(description)
        self.assertTrue(description.group(1).startswith("Use when "))
        for phrase in [
            "explicitly requested or approved",
            "references/configuration.md",
            "python scripts/codereview_client.py",
            "Set that directory as the shell working directory",
            "Do not run the client from the repository being reviewed",
            "selected files",
            "untrusted data",
            "local evidence",
            "up to `1 + MAX_RETRIES` external requests per selected upstream",
            "independent retry counter",
            "python scripts/codereview_client.py doctor",
            "does not perform a network request",
            "must not include the protocol endpoint path",
            "third-party-code-review-skill/1.0",
            "THIRD_PARTY_CODEREVIEW_MAX_RETRIES",
            "visible upstream reasoning or summary",
            "Review result",
            "No review text returned",
        ]:
            self.assertIn(phrase, text)
        self.assert_client_simulation_documented(text)

    def test_readme_is_detailed_and_uses_the_portable_entry_point(self):
        text = README.read_text(encoding="utf-8")

        for heading in [
            "# Third-party Code Review Skill",
            "## Files",
            "## Requirements",
            "## Configuration",
            "## Protocols",
            "### OpenAI Chat Completions",
            "### OpenAI Responses",
            "### Anthropic-compatible Messages",
            "## Usage",
            "## Security Model",
            "## Testing",
            "## Limitations",
        ]:
            self.assertIn(heading, text)
        self.assertIn("python scripts/codereview_client.py", text)
        self.assertNotIn("rtk", text.lower())
        self.assertIn("BASE_URL", text)
        self.assertNotIn("API_PATH", text)
        self.assertNotIn("API_URL", text)
        self.assertNotIn("--url", text)
        self.assertNotIn("MAX_INPUT_CHARS", text)
        self.assertNotIn("--max-input-chars", text)
        self.assertNotIn('STREAM = "auto"', text)
        self.assertIn("must not include the protocol endpoint path", text)
        self.assertIn("third-party-code-review-skill/1.0", text)
        self.assertIn("MAX_RETRIES", text)
        self.assertIn("THIRD_PARTY_CODEREVIEW_MAX_RETRIES", text)
        self.assertIn("independently", text)
        self.assertIn("four attempts", text)
        self.assertIn("unsupported by the active console encoding", text)
        self.assertIn("visible upstream reasoning or summary", text)
        self.assertIn("Review result", text)
        self.assertIn("No review text returned", text)
        self.assert_client_simulation_documented(text)
        for path in ["/v1/chat/completions", "/v1/responses", "/v1/messages"]:
            self.assertIn(path, text)

    def test_openai_yaml_is_utf8_and_mentions_skill(self):
        text = OPENAI_YAML.read_text(encoding="utf-8")

        self.assertNotIn("�", text)
        self.assertIn('display_name: "third-party-code-review-skill"', text)
        self.assertIn("$third-party-code-review-skill", text)

    def test_config_example_is_valid_and_contains_no_real_key(self):
        text = CONFIG_EXAMPLE.read_text(encoding="utf-8")
        parsed = tomllib.loads(text)

        upstreams = [
            value
            for key, value in parsed.items()
            if key.lower().startswith("upstream")
        ]
        self.assertEqual(len(upstreams), 3)
        self.assertTrue(all(not upstream["ENABLED"] for upstream in upstreams))
        self.assertEqual(upstreams[0]["PROTOCOL"], "openai_chat")
        self.assertEqual(
            upstreams[0]["BASE_URL"],
            "http://172.28.100.252:10130/proxy/codereview_chat",
        )
        self.assertEqual(upstreams[0]["MODEL"], "codereview")
        for upstream in upstreams:
            self.assertEqual(upstream["API_KEY"], "")
            self.assertEqual(upstream["TIMEOUT_SECONDS"], 600)
            self.assertEqual(upstream["MAX_RETRIES"], 3)
            self.assertTrue(upstream["STREAM"])
            self.assertIs(upstream["SIMULATED_CLIENT"], False)
        self.assertNotIn("API_URL", text)
        self.assertNotIn("API_BASE", text)
        self.assertNotIn("API_PATH", text)
        self.assertNotIn("CONTEXT_WINDOW_TOKENS", text)
        self.assertNotIn("MAX_OUTPUT_TOKENS", text)
        self.assertNotIn("ANTHROPIC_VERSION", text)
        self.assertNotIn("MAX_INPUT_CHARS", text)
        self.assertNotIn("MAX_CHARS", text)
        self.assertNotRegex(text, r"\bsk-[A-Za-z0-9_-]{12,}\b")
        for protocol in ["openai_chat", "openai_responses", "anthropic"]:
            self.assertIn(protocol, text)
        self.assertNotIn("claude_messages", text)
        for label in ["[UPSTREAM_codereview]", "[UPSTREAM2]", "[UPSTREAM3]"]:
            self.assertIn(label, text)
        self.assertNotIn('STREAM = "auto"', text)
        self.assertIn("Enable streaming by default", text)
        self.assertIn(
            'BASE_URL = "http://172.28.100.252:10130/proxy/codereview_chat"',
            text,
        )
        self.assertIn("explicitly accept the cleartext credential risk", text)
        self.assertIn('BASE_URL = "https://api.example.test"', text)
        self.assertIn(
            'BASE_URL = "https://gateway.example.test"', text
        )
        self.assertNotIn('BASE_URL = "https://api.example.test/v1/responses"', text)
        self.assertNotIn("/claude/messages", text)

    def test_configuration_reference_documents_precedence_and_user_path(self):
        text = CONFIG_REFERENCE.read_text(encoding="utf-8")

        self.assertIn("%USERPROFILE%\\.config\\third-party-code-review-skill\\config.toml", text)
        self.assertIn("THIRD_PARTY_CODEREVIEW_CONFIG", text)
        self.assertIn("Command-line options", text)
        self.assertIn("Plain HTTP", text)
        self.assertIn("no `tools` field", text)
        self.assertIn("THIRD_PARTY_CODEREVIEW_BASE_URL", text)
        self.assertNotIn("THIRD_PARTY_CODEREVIEW_API_PATH", text)
        self.assertNotIn("THIRD_PARTY_CODEREVIEW_URL", text)
        self.assertNotIn("`API_URL`", text)
        self.assertNotIn("CONTEXT_WINDOW_TOKENS", text)
        self.assertNotIn("MAX_OUTPUT_TOKENS", text)
        self.assertNotIn("ANTHROPIC_VERSION", text)
        self.assertNotIn("MAX_INPUT_CHARS", text)
        self.assertNotIn("THIRD_PARTY_CODEREVIEW_MAX_INPUT_CHARS", text)
        self.assertNotIn('STREAM = "auto"', text)
        self.assertIn("first two enabled", text)
        self.assertIn("concurrently", text)
        self.assertIn("Anthropic-compatible Messages", text)
        self.assertIn("python scripts/codereview_client.py doctor", text)
        self.assertIn("does not perform a network request", text)
        self.assertIn("active_config_source", text)
        self.assertIn("request_url", text)
        self.assertIn("must not include the protocol endpoint path", text)
        for path in ["/v1/chat/completions", "/v1/responses", "/v1/messages"]:
            self.assertIn(path, text)
        self.assertIn("third-party-code-review-skill/1.0", text)
        self.assertIn("MAX_RETRIES", text)
        self.assertIn("THIRD_PARTY_CODEREVIEW_MAX_RETRIES", text)
        self.assertIn("independent retry counter", text)
        self.assertIn("up to four attempts", text)
        self.assertIn("unsupported by the active console encoding", text)
        self.assertIn("reasoning_content", text)
        self.assertIn("thinking_delta", text)
        self.assertIn("Review result", text)
        self.assertIn("No review text returned", text)
        for phrase in [
            "`SIMULATED_CLIENT` | `false`; boolean",
            "no environment-variable or command-line override",
            '"simulated_client"',
        ]:
            self.assertIn(phrase, text)
        self.assert_client_simulation_documented(text)

    def test_local_process_files_are_git_ignored(self):
        lines = GITIGNORE.read_text(encoding="utf-8").splitlines()

        self.assertIn("docs/", lines)
        self.assertIn("implementation-notes.md", lines)

if __name__ == "__main__":
    unittest.main()
