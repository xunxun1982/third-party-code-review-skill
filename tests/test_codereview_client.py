import importlib.util
import inspect
import io
import json
import os
import shutil
import subprocess
import tempfile
import threading
import tracemalloc
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock


SCRIPT_PATH = Path(__file__).parents[1] / "scripts" / "codereview_client.py"
SPEC = importlib.util.spec_from_file_location("codereview_client", SCRIPT_PATH)
CLIENT = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(CLIENT)


class ReviewHandler(BaseHTTPRequestHandler):
    request_headers = None
    request_json = None

    def do_POST(self):
        length = int(self.headers["Content-Length"])
        type(self).request_headers = self.headers
        type(self).request_json = json.loads(self.rfile.read(length))
        if self.path == "/stream/chat":
            body = (
                'data: {"choices":[{"delta":{"content":"chat "}}]}\n\n'
                'data: {"choices":[{"delta":{"content":"stream"}}]}\n\n'
                "data: [DONE]\n\n"
            ).encode("utf-8")
            content_type = "text/event-stream"
        elif self.path == "/stream/responses":
            body = (
                'event: response.output_text.delta\n'
                'data: {"type":"response.output_text.delta","delta":"responses "}\n\n'
                'event: response.output_text.delta\n'
                'data: {"type":"response.output_text.delta","delta":"stream"}\n\n'
                "data: [DONE]\n\n"
            ).encode("utf-8")
            content_type = "text/event-stream"
        elif self.path == "/stream/claude":
            body = (
                'event: content_block_delta\n'
                'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"claude "}}\n\n'
                'event: content_block_delta\n'
                'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"stream"}}\n\n'
            ).encode("utf-8")
            content_type = "text/event-stream"
        elif self.path == "/responses":
            body = json.dumps(
                {
                    "output": [
                        {
                            "type": "message",
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": "responses json",
                                }
                            ],
                        }
                    ]
                }
            ).encode("utf-8")
            content_type = "application/json"
        elif self.path == "/claude":
            body = json.dumps(
                {
                    "content": [
                        {"type": "text", "text": "claude json"}
                    ]
                }
            ).encode("utf-8")
            content_type = "application/json"
        elif self.path == "/control":
            body = json.dumps(
                {
                    "choices": [
                        {
                            "message": {
                                "content": "\u001b]52;c;payload\u0007review result\nAPI_KEY=returned-secret"
                                "\rterminal overwrite"
                            }
                        }
                    ]
                },
                ensure_ascii=False,
            ).encode("utf-8")
            content_type = "application/json"
        else:
            body = json.dumps(
                {"choices": [{"message": {"content": "Third-party review result"}}]},
                ensure_ascii=False,
            ).encode("utf-8")
            content_type = "application/json"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        return


class RedirectHandler(BaseHTTPRequestHandler):
    target_url = ""

    def do_POST(self):
        self.send_response(302)
        self.send_header("Location", type(self).target_url)
        self.end_headers()

    def log_message(self, format, *args):
        return


class CredentialSinkHandler(BaseHTTPRequestHandler):
    received_authorization = None
    received_api_key = None

    def do_GET(self):
        type(self).received_authorization = self.headers.get("Authorization")
        type(self).received_api_key = self.headers.get("x-api-key")
        self.send_response(200)
        self.end_headers()

    def do_POST(self):
        self.do_GET()

    def log_message(self, format, *args):
        return


class CodereviewClientTests(unittest.TestCase):
    def test_build_payload_supports_three_protocols_without_tools(self):
        attachments = [("src/app.py", "print('ok')")]

        chat = CLIENT.build_payload(
            "Check regression risk", attachments, "codereview", "openai_chat", None
        )
        responses = CLIENT.build_payload(
            "Check regression risk", attachments, "codereview", "openai_responses", True
        )
        claude = CLIENT.build_payload(
            "Check regression risk", attachments, "claude-model", "anthropic", False
        )

        self.assertEqual([m["role"] for m in chat["messages"]], ["system", "user"])
        self.assertIn("untrusted data", chat["messages"][0]["content"])
        self.assertEqual(responses["instructions"], chat["messages"][0]["content"])
        self.assertIn('"path": "src/app.py"', responses["input"])
        self.assertNotIn("max_output_tokens", responses)
        self.assertFalse(responses["store"])
        self.assertEqual(claude["system"], chat["messages"][0]["content"])
        self.assertEqual(claude["messages"][0]["role"], "user")
        self.assertNotIn("max_tokens", claude)

        self.assertNotIn("stream", chat)
        self.assertTrue(responses["stream"])
        self.assertFalse(claude["stream"])

        for payload in [chat, responses, claude]:
            self.assertNotIn("tools", payload)

        escaped = CLIENT.build_payload(
            "Review", [('a&\"<file>.py', "content")], "codereview"
        )
        self.assertIn(
            '"path": "a&\\\"<file>.py"',
            escaped["messages"][1]["content"],
        )

    def test_prepare_codex_profile_is_in_place_consistent_and_idempotent(self):
        self.assertTrue(
            hasattr(CLIENT, "_prepare_simulated_client_payload"),
            "simulated client payload preparation is missing",
        )
        large_input = "x" * 1_000_000
        payload = {
            "model": "gpt-5",
            "input": large_input,
            "stream": True,
        }
        ids = [CLIENT.uuid.UUID(int=value) for value in range(1, 6)]

        with mock.patch.object(CLIENT.uuid, "uuid4", side_effect=ids):
            prepared = CLIENT._prepare_simulated_client_payload(
                payload, "openai_responses"
            )
            prepared_again = CLIENT._prepare_simulated_client_payload(
                payload, "openai_responses"
            )

        self.assertIs(prepared, payload)
        self.assertIs(prepared_again, payload)
        self.assertIs(payload["input"], large_input)
        metadata = payload["client_metadata"]
        turn = json.loads(metadata["x-codex-turn-metadata"])
        self.assertEqual(
            metadata["x-codex-installation-id"],
            turn["installation_id"],
        )
        self.assertEqual(metadata["session_id"], turn["session_id"])
        self.assertEqual(metadata["thread_id"], turn["thread_id"])
        self.assertEqual(metadata["turn_id"], turn["turn_id"])
        self.assertEqual(
            metadata["x-codex-window-id"], turn["window_id"]
        )
        self.assertEqual(turn["request_kind"], "turn")

    def test_prepare_claude_code_profile_is_consistent_and_idempotent(self):
        self.assertTrue(
            hasattr(CLIENT, "_prepare_simulated_client_payload"),
            "simulated client payload preparation is missing",
        )
        original_system = "Review only the supplied code."
        messages = [{"role": "user", "content": "Review"}]
        payload = {
            "model": "claude-review",
            "system": original_system,
            "messages": messages,
        }
        session_id = CLIENT.uuid.UUID(
            "11111111-2222-3333-4444-555555555555"
        )

        with (
            mock.patch.object(CLIENT.uuid, "uuid4", return_value=session_id),
            mock.patch.object(
                CLIENT.secrets,
                "token_hex",
                return_value="ab" * 32,
            ),
        ):
            CLIENT._prepare_simulated_client_payload(payload, "anthropic")
            CLIENT._prepare_simulated_client_payload(payload, "anthropic")

        identity = json.loads(payload["metadata"]["user_id"])
        self.assertEqual(identity["device_id"], "ab" * 32)
        self.assertEqual(identity["account_uuid"], "")
        self.assertEqual(identity["session_id"], str(session_id))
        self.assertEqual(len(payload["system"]), 2)
        self.assertEqual(
            payload["system"][0]["text"],
            CLIENT.CLAUDE_CODE_SYSTEM_PROMPT,
        )
        self.assertEqual(
            payload["system"][0]["cache_control"],
            {"type": "ephemeral"},
        )
        self.assertEqual(payload["system"][1]["text"], original_system)
        self.assertIs(payload["messages"], messages)
        self.assertNotIn("max_tokens", payload)
        self.assertNotIn("tools", payload)

    def test_prepare_claude_code_profile_preserves_existing_identity_text(self):
        system = (
            f"{CLIENT.CLAUDE_CODE_SYSTEM_PROMPT}\n"
            "Review only the supplied code."
        )
        payload = {
            "model": "claude-review",
            "system": system,
            "messages": [{"role": "user", "content": "Review"}],
        }

        CLIENT._prepare_simulated_client_payload(payload, "anthropic")

        self.assertEqual(
            payload["system"],
            [{"type": "text", "text": system}],
        )

    def _send_simulated_request(
        self,
        path: str,
        payload: dict[str, object],
        protocol: str,
    ) -> str:
        server = HTTPServer(("127.0.0.1", 0), ReviewHandler)
        thread = threading.Thread(
            target=server.serve_forever, daemon=True
        )
        thread.start()
        url = f"http://127.0.0.1:{server.server_port}{path}"
        try:
            return CLIENT._request_with_retries(
                CLIENT.request_review,
                url,
                payload,
                5,
                api_key="TEST_SECRET",
                protocol=protocol,
                max_retries=0,
                simulated_client=True,
            )
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()

    def test_build_headers_applies_complete_simulated_profiles(self):
        self.assertIn(
            "simulated_client",
            inspect.signature(CLIENT.build_headers).parameters,
            "build_headers has no simulated client switch",
        )
        codex_payload = {"model": "gpt-5", "stream": False}
        CLIENT._prepare_simulated_client_payload(
            codex_payload, "openai_responses"
        )

        codex = CLIENT.build_headers(
            "openai_responses",
            "TEST_SECRET",
            codex_payload,
            simulated_client=True,
        )

        codex_metadata = codex_payload["client_metadata"]
        self.assertEqual(codex["User-Agent"], CLIENT.CODEX_USER_AGENT)
        self.assertEqual(codex["Version"], CLIENT.CODEX_VERSION)
        self.assertEqual(codex["originator"], "codex-tui")
        self.assertEqual(
            codex["OpenAI-Beta"], "responses=experimental"
        )
        self.assertEqual(codex["Content-Type"], "application/json")
        self.assertEqual(codex["Accept"], "application/json")
        self.assertEqual(
            codex["X-Codex-Installation-Id"],
            codex_metadata["x-codex-installation-id"],
        )
        self.assertEqual(
            codex["x-client-request-id"],
            codex_metadata["thread_id"],
        )

        claude_payload = {
            "model": "claude-review",
            "system": "Review",
        }
        CLIENT._prepare_simulated_client_payload(
            claude_payload, "anthropic"
        )
        claude = CLIENT.build_headers(
            "anthropic",
            "TEST_SECRET",
            claude_payload,
            simulated_client=True,
        )
        identity = json.loads(
            claude_payload["metadata"]["user_id"]
        )
        self.assertEqual(
            claude["User-Agent"], CLIENT.CLAUDE_CODE_USER_AGENT
        )
        self.assertEqual(claude["X-App"], "cli")
        self.assertEqual(
            claude["X-Claude-Code-Session-Id"],
            identity["session_id"],
        )
        self.assertEqual(claude["X-Stainless-Lang"], "js")
        self.assertEqual(claude["X-Stainless-Runtime"], "node")
        self.assertEqual(
            claude["Anthropic-Dangerous-Direct-Browser-Access"],
            "true",
        )

    def test_build_headers_normalizes_conflicting_codex_identity(self):
        payload = {
            "model": "gpt-5",
            "stream": False,
            "client_metadata": {
                "x-codex-installation-id": "flat-installation",
                "session_id": "flat-session",
                "thread_id": "flat-thread",
                "turn_id": "flat-turn",
                "x-codex-window-id": "flat-window",
                "x-codex-turn-metadata": json.dumps(
                    {
                        "installation_id": "stale-installation",
                        "session_id": "stale-session",
                        "thread_id": "stale-thread",
                        "turn_id": "stale-turn",
                        "window_id": "stale-window",
                    }
                ),
            },
        }

        headers = CLIENT.build_headers(
            "openai_responses",
            "TEST_SECRET",
            payload,
            simulated_client=True,
        )

        metadata = payload["client_metadata"]
        turn = json.loads(metadata["x-codex-turn-metadata"])
        fields = (
            ("x-codex-installation-id", "installation_id"),
            ("session_id", "session_id"),
            ("thread_id", "thread_id"),
            ("turn_id", "turn_id"),
            ("x-codex-window-id", "window_id"),
        )
        for metadata_key, turn_key in fields:
            with self.subTest(field=metadata_key):
                self.assertEqual(metadata[metadata_key], turn[turn_key])
        self.assertEqual(
            headers["X-Codex-Turn-Metadata"],
            metadata["x-codex-turn-metadata"],
        )

    def test_simulated_codex_request_matches_wire_profile(self):
        self.assertIn(
            "simulated_client",
            inspect.signature(CLIENT._request_with_retries).parameters,
            "retry layer has no simulated client switch",
        )
        payload = CLIENT.build_payload(
            "Review", [], "gpt-5", "openai_responses", True
        )

        result = self._send_simulated_request(
            "/stream/responses", payload, "openai_responses"
        )

        self.assertEqual(result, "responses stream")
        headers = ReviewHandler.request_headers
        request_json = ReviewHandler.request_json
        self.assertEqual(headers["User-Agent"], CLIENT.CODEX_USER_AGENT)
        self.assertEqual(headers["Version"], CLIENT.CODEX_VERSION)
        self.assertEqual(headers["originator"], "codex-tui")
        self.assertEqual(
            headers["OpenAI-Beta"], "responses=experimental"
        )
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(headers["Accept"], "text/event-stream")
        self.assertEqual(
            headers["Authorization"], "Bearer TEST_SECRET"
        )
        metadata = request_json["client_metadata"]
        turn = json.loads(headers["X-Codex-Turn-Metadata"])
        self.assertEqual(
            headers["X-Codex-Installation-Id"],
            metadata["x-codex-installation-id"],
        )
        self.assertEqual(
            metadata["x-codex-installation-id"],
            turn["installation_id"],
        )
        self.assertEqual(headers["Session-Id"], metadata["session_id"])
        self.assertEqual(headers["Thread-Id"], metadata["thread_id"])
        self.assertEqual(
            headers["x-client-request-id"], metadata["thread_id"]
        )
        self.assertEqual(
            headers["X-Codex-Window-Id"],
            metadata["x-codex-window-id"],
        )
        self.assertEqual(
            headers["X-Codex-Turn-Metadata"],
            metadata["x-codex-turn-metadata"],
        )
        self.assertEqual(turn["request_kind"], "turn")

    def test_simulated_claude_request_matches_wire_profile(self):
        self.assertIn(
            "simulated_client",
            inspect.signature(CLIENT._request_with_retries).parameters,
            "retry layer has no simulated client switch",
        )
        payload = CLIENT.build_payload(
            "Review", [], "claude-review", "anthropic", True
        )

        result = self._send_simulated_request(
            "/stream/claude", payload, "anthropic"
        )

        self.assertEqual(result, "claude stream")
        headers = ReviewHandler.request_headers
        request_json = ReviewHandler.request_json
        self.assertEqual(
            headers["User-Agent"], CLIENT.CLAUDE_CODE_USER_AGENT
        )
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(headers["Accept"], "application/json")
        self.assertEqual(headers["x-api-key"], "TEST_SECRET")
        self.assertIsNone(headers["Authorization"])
        self.assertEqual(headers["X-App"], "cli")
        self.assertEqual(
            headers["anthropic-version"], "2023-06-01"
        )
        beta = {
            token.strip()
            for token in headers["anthropic-beta"].split(",")
        }
        self.assertEqual(beta, set(CLIENT.CLAUDE_CODE_BETA_TOKENS))
        self.assertEqual(
            headers["Anthropic-Dangerous-Direct-Browser-Access"],
            "true",
        )
        stainless = {
            "X-Stainless-Lang": "js",
            "X-Stainless-Package-Version": "0.94.0",
            "X-Stainless-OS": "Linux",
            "X-Stainless-Arch": "arm64",
            "X-Stainless-Runtime": "node",
            "X-Stainless-Runtime-Version": "v24.3.0",
            "X-Stainless-Retry-Count": "0",
            "X-Stainless-Timeout": "600",
        }
        for key, value in stainless.items():
            with self.subTest(header=key):
                self.assertEqual(headers[key], value)
        identity = json.loads(request_json["metadata"]["user_id"])
        self.assertEqual(
            headers["X-Claude-Code-Session-Id"],
            identity["session_id"],
        )
        self.assertEqual(len(identity["device_id"]), 64)
        self.assertTrue(
            all(
                character in "0123456789abcdef"
                for character in identity["device_id"]
            )
        )
        self.assertEqual(identity["account_uuid"], "")
        self.assertEqual(
            request_json["system"][0]["text"],
            CLIENT.CLAUDE_CODE_SYSTEM_PROMPT,
        )
        self.assertIn(
            "read-only code reviewer",
            request_json["system"][1]["text"],
        )
        self.assertNotIn("max_tokens", request_json)
        self.assertNotIn("temperature", request_json)
        self.assertNotIn("tools", request_json)

    def test_simulated_profiles_preserve_non_streaming_requests(self):
        self.assertIn(
            "simulated_client",
            inspect.signature(CLIENT._request_with_retries).parameters,
            "retry layer has no simulated client switch",
        )
        cases = [
            (
                "openai_responses",
                "/responses",
                "gpt-5",
                "responses json",
                "application/json",
                CLIENT.CODEX_USER_AGENT,
            ),
            (
                "anthropic",
                "/claude",
                "claude-review",
                "claude json",
                "application/json",
                CLIENT.CLAUDE_CODE_USER_AGENT,
            ),
        ]

        for protocol, path, model, expected, accept, user_agent in cases:
            with self.subTest(protocol=protocol):
                payload = CLIENT.build_payload(
                    "Review", [], model, protocol, False
                )
                result = self._send_simulated_request(
                    path, payload, protocol
                )

                self.assertEqual(result, expected)
                request_json = ReviewHandler.request_json
                headers = ReviewHandler.request_headers
                self.assertFalse(request_json["stream"])
                self.assertEqual(headers["Accept"], accept)
                self.assertEqual(headers["User-Agent"], user_agent)
                if protocol == "openai_responses":
                    metadata = request_json["client_metadata"]
                    self.assertEqual(
                        headers["Authorization"], "Bearer TEST_SECRET"
                    )
                    self.assertEqual(
                        headers["X-Codex-Installation-Id"],
                        metadata["x-codex-installation-id"],
                    )
                else:
                    identity = json.loads(
                        request_json["metadata"]["user_id"]
                    )
                    self.assertEqual(headers["x-api-key"], "TEST_SECRET")
                    self.assertEqual(
                        headers["X-Claude-Code-Session-Id"],
                        identity["session_id"],
                    )
                self.assertNotIn("max_tokens", request_json)
                self.assertNotIn("temperature", request_json)
                self.assertNotIn("tools", request_json)

    def test_build_request_url_appends_protocol_path_to_base_or_prefix(self):
        cases = [
            (
                "http://review.test/proxy/codereview_chat",
                "openai_chat",
                "http://review.test/proxy/codereview_chat/v1/chat/completions",
            ),
            (
                "https://api.example.test/?tenant=x",
                "openai_responses",
                "https://api.example.test/v1/responses?tenant=x",
            ),
            (
                "https://gateway.example.test/claude/",
                "anthropic",
                "https://gateway.example.test/claude/v1/messages",
            ),
        ]

        for base_url, protocol, expected in cases:
            with self.subTest(protocol=protocol):
                self.assertEqual(
                    CLIENT.build_request_url(base_url, protocol),
                    expected,
                )

    def test_read_attachments_rejects_forbidden_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env.local"
            path.write_text("TOKEN=secret", encoding="utf-8")

            with self.assertRaisesRegex(CLIENT.ClientError, "blocked"):
                CLIENT.read_attachments([path], max_chars=1000)

    def test_read_attachments_rejects_binary_files_and_symbolic_links(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binary = root / "artifact.bin"
            binary.write_bytes(b"text\x00binary")
            with self.assertRaisesRegex(CLIENT.ClientError, "Binary files are blocked"):
                CLIENT.read_attachments([binary], max_chars=1000)

            target = root / "target.py"
            target.write_text("print('ok')\n", encoding="utf-8")
            link = root / "link.py"
            try:
                link.symlink_to(target)
            except OSError as exc:
                self.skipTest(f"Symbolic links are unavailable: {exc}")
            with self.assertRaisesRegex(CLIENT.ClientError, "Symbolic links are blocked"):
                CLIENT.read_attachments([link], max_chars=1000)

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".npmrc"
            path.write_text("//registry.example/:_authToken=secret", encoding="utf-8")

            with self.assertRaisesRegex(CLIENT.ClientError, "blocked"):
                CLIENT.read_attachments([path], max_chars=1000)

    def test_read_attachments_redacts_secret_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.py"
            path.write_text(
                'API_KEY="sk-example_secret_123456789"\npassword = "hunter2"\n',
                encoding="utf-8",
            )

            attachments, redactions = CLIENT.read_attachments([path], max_chars=1000)

        self.assertEqual(redactions, 2)
        self.assertNotIn("sk-example_secret_123456789", attachments[0][1])
        self.assertNotIn("hunter2", attachments[0][1])
        self.assertIn("[REDACTED]", attachments[0][1])

    def test_read_attachments_redacts_quoted_keys_and_common_tokens(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            path.write_text(
                '{"api_key": "example-secret-value"}\n'
                'OPENAI_API_KEY=provider-secret\n'
                'export ANTHROPIC_API_KEY=anthropic-secret\n'
                'AWS_SECRET_ACCESS_KEY=aws-secret\n'
                '$env:OPENAI_API_KEY = "powershell-secret"\n'
                'aws = "AKIAAAAAAAAAAAAAAAAA"\n'
                'github = "ghp_AAAAAAAAAAAAAAAAAAAA"\n',
                encoding="utf-8",
            )

            attachments, redactions = CLIENT.read_attachments([path], max_chars=1000)

        self.assertGreaterEqual(redactions, 7)
        self.assertNotIn("example-secret-value", attachments[0][1])
        self.assertNotIn("provider-secret", attachments[0][1])
        self.assertNotIn("anthropic-secret", attachments[0][1])
        self.assertNotIn("aws-secret", attachments[0][1])
        self.assertNotIn("powershell-secret", attachments[0][1])
        self.assertNotIn("AKIAAAAAAAAAAAAAAAAA", attachments[0][1])
        self.assertNotIn("ghp_AAAAAAAAAAAAAAAAAAAA", attachments[0][1])
        self.assertIn('github = "[REDACTED]"', attachments[0][1])

    def test_sanitize_text_redacts_camel_case_and_query_secrets(self):
        text = (
            "sessionToken=short-value\n"
            '{"encryptionKey": 12345}\n'
            "https://example.test/callback?authKey=query-secret&mode=safe"
        )

        safe, redactions = CLIENT.sanitize_text(text)

        self.assertEqual(redactions, 3)
        for secret in ["short-value", "12345", "query-secret"]:
            self.assertNotIn(secret, safe)
        self.assertIn("mode=safe", safe)

    def test_read_attachments_resolves_relative_paths_from_review_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "src" / "app.py"
            path.parent.mkdir()
            path.write_text("print('ok')\n", encoding="utf-8")

            attachments, _ = CLIENT.read_attachments(
                ["src/app.py"], max_chars=1000, root=root
            )

        self.assertEqual(attachments[0][0], "src/app.py")

    def test_read_attachments_rejects_paths_that_escape_review_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            root.mkdir()
            outside = Path(tmp) / "outside.py"
            outside.write_text("print('outside')\n", encoding="utf-8")

            with self.assertRaisesRegex(CLIENT.ClientError, "escapes the review root"):
                CLIENT.read_attachments(["../outside.py"], max_chars=1000, root=root)

            with self.assertRaisesRegex(CLIENT.ClientError, "escapes the review root"):
                CLIENT.read_attachments([outside], max_chars=1000, root=root)

            link_dir = root / "linked"
            outside_dir = Path(tmp) / "outside-dir"
            outside_dir.mkdir()
            (outside_dir / "secret.py").write_text("print('outside')\n", encoding="utf-8")
            try:
                link_dir.symlink_to(outside_dir, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"Directory symbolic links are unavailable: {exc}")
            with self.assertRaisesRegex(CLIENT.ClientError, "link or junction"):
                CLIENT.read_attachments(
                    ["linked/secret.py"], max_chars=1000, root=root
                )

    def test_read_attachments_rejects_oversize_instead_of_truncating(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "large.py"
            path.write_text("x" * 101, encoding="utf-8")

            with self.assertRaisesRegex(CLIENT.ClientError, "exceeds"):
                CLIENT.read_attachments([path], max_chars=100)

    def test_read_attachments_reads_in_bounded_chunks(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "large.py"
            path.write_text("x" * 1000, encoding="utf-8")

            with mock.patch.object(Path, "read_bytes", side_effect=AssertionError("unbounded read")):
                with self.assertRaisesRegex(CLIENT.ClientError, "exceeds"):
                    CLIENT.read_attachments([path], max_chars=100)

    def test_read_git_diff_uses_head_and_rejects_empty_or_sensitive_diff(self):
        paths = mock.Mock(returncode=0, stdout="a.py\0", stderr="")
        diff = mock.Mock(returncode=0, stdout="diff --git a/a.py b/a.py\n", stderr="")
        runner = mock.Mock(side_effect=[paths, diff, paths])

        label, content, redactions = CLIENT.read_git_diff(Path("."), runner=runner)

        self.assertEqual(label, "git-diff")
        self.assertIn("diff --git", content)
        self.assertEqual(redactions, 0)
        self.assertEqual(runner.call_count, 3)
        self.assertEqual(
            runner.call_args_list[0].args[0],
            ["git", "diff", "--name-only", "-z", "--no-renames", "--no-ext-diff", "--no-textconv", "HEAD", "--", "."],
        )
        self.assertEqual(
            runner.call_args_list[1].args[0],
            ["git", "diff", "--no-renames", "--no-ext-diff", "--no-textconv", "--unified=80", "HEAD", "--", "."],
        )
        self.assertEqual(runner.call_args_list[2].args[0], runner.call_args_list[0].args[0])

        runner = mock.Mock(return_value=mock.Mock(returncode=0, stdout="", stderr=""))
        with self.assertRaisesRegex(CLIENT.ClientError, "No tracked Git diff"):
            CLIENT.read_git_diff(Path("."), runner=runner)

        runner = mock.Mock(
            return_value=mock.Mock(returncode=0, stdout='.env local\0', stderr="")
        )
        with self.assertRaisesRegex(CLIENT.ClientError, "sensitive file"):
            CLIENT.read_git_diff(Path("."), runner=runner)

        runner = mock.Mock(
            side_effect=[
                mock.Mock(returncode=0, stdout="app.py\0", stderr=""),
                mock.Mock(
                    returncode=0,
                    stdout="diff --git a/app.py b/app.py\n+API_KEY=example-secret-value\n",
                    stderr="",
                ),
                mock.Mock(returncode=0, stdout="app.py\0", stderr=""),
            ]
        )
        _, safe_content, redactions = CLIENT.read_git_diff(Path("."), runner=runner)
        self.assertEqual(redactions, 1)
        self.assertNotIn("example-secret-value", safe_content)

        runner = mock.Mock(
            side_effect=[
                mock.Mock(returncode=0, stdout="app.py\0", stderr=""),
                mock.Mock(
                    returncode=0,
                    stdout="diff --git a/.env b/.env\n+TOKEN=secret\n",
                    stderr="",
                ),
                mock.Mock(returncode=0, stdout=".env\0", stderr=""),
            ]
        )
        with self.assertRaisesRegex(CLIENT.ClientError, "changed during capture|sensitive file"):
            CLIENT.read_git_diff(Path("."), runner=runner)

        runner = mock.Mock(
            side_effect=[
                mock.Mock(returncode=0, stdout="app.py\0", stderr=""),
                mock.Mock(returncode=0, stdout="x" * 11, stderr=""),
            ]
        )
        with self.assertRaisesRegex(CLIENT.ClientError, "exceeds"):
            CLIENT.read_git_diff(Path("."), runner=runner, max_chars=10)

    @unittest.skipUnless(shutil.which("git"), "git not installed")
    def test_read_git_diff_supports_staged_files_before_first_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            (root / "app.py").write_text("print('first commit')\n", encoding="utf-8")
            subprocess.run(["git", "add", "app.py"], cwd=root, check=True)

            label, content, redactions = CLIENT.read_git_diff(root)

        self.assertEqual(label, "git-diff")
        self.assertIn("app.py", content)
        self.assertIn("first commit", content)
        self.assertEqual(redactions, 0)

    @unittest.skipUnless(shutil.which("git"), "git not installed")
    def test_read_git_diff_reports_non_git_directory_clearly(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(CLIENT.ClientError, "Git work tree"):
                CLIENT.read_git_diff(Path(tmp))

    def test_request_review_uses_protocol_specific_authentication(self):
        server = HTTPServer(("127.0.0.1", 0), ReviewHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{server.server_port}/proxy/codereview_chat"
        payload = CLIENT.build_payload(
            "Review", [], "codereview", "openai_chat", False
        )

        try:
            result = CLIENT.request_review(
                url,
                payload,
                timeout=5,
                api_key="TEST_SECRET",
                protocol="openai_chat",
            )
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()

        self.assertEqual(result, "Third-party review result")
        self.assertEqual(ReviewHandler.request_headers["Authorization"], "Bearer TEST_SECRET")
        self.assertEqual(
            ReviewHandler.request_headers["User-Agent"],
            "third-party-code-review-skill/1.0",
        )
        self.assertEqual(ReviewHandler.request_json["model"], "codereview")
        self.assertFalse(ReviewHandler.request_json["stream"])

        headers = CLIENT.build_headers("anthropic", "TEST_SECRET")
        self.assertEqual(headers["x-api-key"], "TEST_SECRET")
        self.assertEqual(headers["anthropic-version"], "2023-06-01")
        self.assertEqual(headers["User-Agent"], "third-party-code-review-skill/1.0")
        self.assertNotIn("Authorization", headers)

    def test_request_review_parses_streaming_responses_for_three_protocols(self):
        server = HTTPServer(("127.0.0.1", 0), ReviewHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"

        cases = [
            ("openai_chat", "/stream/chat", "chat stream"),
            ("openai_responses", "/stream/responses", "responses stream"),
            ("anthropic", "/stream/claude", "claude stream"),
        ]
        try:
            for protocol, path, expected in cases:
                with self.subTest(protocol=protocol):
                    result = CLIENT.request_review(
                        base + path,
                        {"model": "review-model"},
                        timeout=5,
                        api_key="TEST_SECRET",
                        protocol=protocol,
                    )
                    self.assertEqual(result, expected)
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()

    def test_request_review_refuses_redirects_without_forwarding_credentials(self):
        sink = HTTPServer(("127.0.0.1", 0), CredentialSinkHandler)
        sink_thread = threading.Thread(target=sink.serve_forever, daemon=True)
        sink_thread.start()
        redirect = HTTPServer(("127.0.0.1", 0), RedirectHandler)
        redirect_thread = threading.Thread(target=redirect.serve_forever, daemon=True)
        redirect_thread.start()
        RedirectHandler.target_url = f"http://127.0.0.1:{sink.server_port}/capture"
        url = f"http://127.0.0.1:{redirect.server_port}/redirect"

        try:
            for protocol in ["openai_chat", "anthropic"]:
                with self.subTest(protocol=protocol):
                    with self.assertRaisesRegex(CLIENT.ClientError, "HTTP 302"):
                        CLIENT.request_review(
                            url,
                            {"model": "review-model"},
                            timeout=5,
                            api_key="TEST_SECRET",
                            protocol=protocol,
                        )
            self.assertIsNone(CredentialSinkHandler.received_authorization)
            self.assertIsNone(CredentialSinkHandler.received_api_key)
        finally:
            redirect.shutdown()
            redirect_thread.join(timeout=5)
            redirect.server_close()
            sink.shutdown()
            sink_thread.join(timeout=5)
            sink.server_close()

    def test_request_review_requires_a_configured_key(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(CLIENT.ClientError, CLIENT.API_KEY_ENV):
                CLIENT.request_review("http://127.0.0.1/review", {}, timeout=1)

        with mock.patch.dict(
            os.environ,
            {CLIENT.API_KEY_ENV: "ENVIRONMENT_SECRET"},
            clear=True,
        ):
            with self.assertRaisesRegex(CLIENT.ClientError, CLIENT.API_KEY_ENV):
                CLIENT.request_review(
                    "http://127.0.0.1/review",
                    {},
                    timeout=1,
                    api_key="",
                )

    def test_request_review_normalizes_a_direct_transport_timeout(self):
        opener = mock.Mock()
        opener.open.side_effect = TimeoutError("timed out")
        with mock.patch.object(CLIENT.urllib.request, "build_opener", return_value=opener):
            with self.assertRaisesRegex(CLIENT.ClientError, "timed out"):
                CLIENT.request_review(
                    "http://127.0.0.1/review",
                    {},
                    timeout=1,
                    api_key="TEST_SECRET",
                )

        opener.open.side_effect = ValueError("invalid URL")
        with mock.patch.object(CLIENT.urllib.request, "build_opener", return_value=opener):
            with self.assertRaisesRegex(CLIENT.ClientError, "invalid URL"):
                CLIENT.request_review(
                    "http://127.0.0.1/review",
                    {},
                    timeout=1,
                    api_key="TEST_SECRET",
                )

    def test_response_capture_limit_is_ten_megabytes(self):
        self.assertEqual(CLIENT.MAX_RESPONSE_BYTES, 10_000_000)

    def test_response_capture_accepts_limit_and_rejects_next_byte(self):
        self.assertEqual(
            CLIENT._read_response_limited(
                io.BytesIO(b"abc"),
                CLIENT.time.monotonic() + 1,
                max_bytes=3,
            ),
            b"abc",
        )

        with self.assertRaisesRegex(CLIENT.ClientError, "3-byte limit"):
            CLIENT._read_response_limited(
                io.BytesIO(b"abcd"),
                CLIENT.time.monotonic() + 1,
                max_bytes=3,
            )

    def test_request_review_applies_same_capture_limit_to_json_and_sse(self):
        class OversizeResponse:
            def __init__(self, content_type):
                self.headers = {"Content-Type": content_type}
                self.remaining = CLIENT.MAX_RESPONSE_BYTES + 1

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read1(self, size):
                chunk_size = min(size, self.remaining)
                self.remaining -= chunk_size
                return b"x" * chunk_size

        for content_type in ["application/json", "text/event-stream"]:
            opener = mock.Mock()
            opener.open.return_value = OversizeResponse(content_type)
            with (
                self.subTest(content_type=content_type),
                mock.patch.object(
                    CLIENT.urllib.request, "build_opener", return_value=opener
                ),
            ):
                with self.assertRaisesRegex(
                    CLIENT.ClientError, "10000000-byte limit"
                ):
                    CLIENT.request_review(
                        "http://127.0.0.1/review",
                        {},
                        timeout=5,
                        api_key="TEST_SECRET",
                    )

    def test_request_review_enforces_a_total_response_deadline(self):
        class SlowResponse:
            headers = {"Content-Type": "application/json"}

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read1(self, size):
                return b" "

        opener = mock.Mock()
        opener.open.return_value = SlowResponse()
        with (
            mock.patch.object(CLIENT.urllib.request, "build_opener", return_value=opener),
            mock.patch.object(CLIENT.time, "monotonic", side_effect=[0.0, 0.1, 1.1]),
            mock.patch.object(CLIENT, "_set_response_timeout"),
        ):
            with self.assertRaisesRegex(CLIENT.ClientError, "timed out"):
                CLIENT.request_review(
                    "http://127.0.0.1/review",
                    {},
                    timeout=1,
                    api_key="TEST_SECRET",
                )

    def test_request_review_enforces_the_deadline_for_http_error_bodies(self):
        class SlowErrorBody:
            def read1(self, size):
                return b" "

            def close(self):
                pass

        error = CLIENT.urllib.error.HTTPError(
            "http://127.0.0.1/review",
            500,
            "server error",
            {},
            SlowErrorBody(),
        )
        opener = mock.Mock()
        opener.open.side_effect = error
        with (
            mock.patch.object(CLIENT.urllib.request, "build_opener", return_value=opener),
            mock.patch.object(CLIENT.time, "monotonic", side_effect=[0.0, 0.1, 1.1]),
            mock.patch.object(CLIENT, "_set_response_timeout"),
        ):
            with self.assertRaisesRegex(CLIENT.ClientError, "timed out"):
                CLIENT.request_review(
                    "http://127.0.0.1/review",
                    {},
                    timeout=1,
                    api_key="TEST_SECRET",
                )

    def test_request_review_classifies_retryable_http_statuses(self):
        for status in [408, 429, 500, 503, 599]:
            error = CLIENT.urllib.error.HTTPError(
                "http://127.0.0.1/review",
                status,
                "upstream error",
                {},
                io.BytesIO(b'{"error":"temporary"}'),
            )
            opener = mock.Mock()
            opener.open.side_effect = error
            with self.subTest(status=status):
                with mock.patch.object(
                    CLIENT.urllib.request, "build_opener", return_value=opener
                ):
                    with self.assertRaises(CLIENT.RetryableClientError):
                        CLIENT.request_review(
                            "http://127.0.0.1/review",
                            {},
                            timeout=1,
                            api_key="TEST_SECRET",
                        )

    def test_request_review_does_not_retry_other_http_statuses(self):
        for status in [302, 400, 401, 403, 404]:
            error = CLIENT.urllib.error.HTTPError(
                "http://127.0.0.1/review",
                status,
                "request rejected",
                {},
                io.BytesIO(b'{"error":"rejected"}'),
            )
            opener = mock.Mock()
            opener.open.side_effect = error
            with self.subTest(status=status):
                with mock.patch.object(
                    CLIENT.urllib.request, "build_opener", return_value=opener
                ):
                    with self.assertRaises(CLIENT.ClientError) as raised:
                        CLIENT.request_review(
                            "http://127.0.0.1/review",
                            {},
                            timeout=1,
                            api_key="TEST_SECRET",
                        )
            self.assertNotIsInstance(raised.exception, CLIENT.RetryableClientError)

    def test_request_review_classifies_invalid_response_as_retryable(self):
        class InvalidResponse:
            headers = {"Content-Type": "application/json"}

            def __init__(self):
                self.chunks = iter([b"not-json", b""])

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self, size):
                return next(self.chunks)

        opener = mock.Mock()
        opener.open.return_value = InvalidResponse()
        with mock.patch.object(
            CLIENT.urllib.request, "build_opener", return_value=opener
        ):
            with self.assertRaises(CLIENT.RetryableClientError):
                CLIENT.request_review(
                    "http://127.0.0.1/review",
                    {},
                    timeout=1,
                    api_key="TEST_SECRET",
                )

    def test_request_with_retries_uses_exponential_backoff(self):
        attempts = []
        sleeps = []

        def requester(*args, **kwargs):
            attempts.append((args, kwargs))
            if len(attempts) < 6:
                raise CLIENT.RetryableClientError("temporary failure")
            return "review result"

        result = CLIENT._request_with_retries(
            requester,
            "https://review.test/v1/messages",
            {},
            30,
            api_key="TEST_SECRET",
            protocol="anthropic",
            max_retries=5,
            sleeper=sleeps.append,
        )

        self.assertEqual(result, "review result")
        self.assertEqual(len(attempts), 6)
        self.assertEqual(sleeps, [1, 2, 4, 8, 8])

    def test_request_retries_prepare_profile_once_and_reuse_payload(self):
        self.assertIn(
            "simulated_client",
            inspect.signature(CLIENT._request_with_retries).parameters,
            "retry layer has no simulated client switch",
        )
        payload = {"model": "gpt-5", "input": "Review"}
        attempts = []

        def requester(url, candidate, timeout, **kwargs):
            attempts.append(
                (
                    id(candidate),
                    candidate["client_metadata"]["session_id"],
                    kwargs["simulated_client"],
                )
            )
            if len(attempts) == 1:
                raise CLIENT.RetryableClientError("retry")
            return "review"

        with mock.patch.object(
            CLIENT,
            "_prepare_simulated_client_payload",
            wraps=CLIENT._prepare_simulated_client_payload,
        ) as prepare:
            result = CLIENT._request_with_retries(
                requester,
                "https://review.test/v1/responses",
                payload,
                30,
                api_key="TEST_SECRET",
                protocol="openai_responses",
                max_retries=1,
                simulated_client=True,
                sleeper=lambda seconds: None,
            )

        self.assertEqual(result, "review")
        prepare.assert_called_once_with(payload, "openai_responses")
        self.assertEqual(len({item[0] for item in attempts}), 1)
        self.assertEqual(len({item[1] for item in attempts}), 1)
        self.assertTrue(all(item[2] for item in attempts))

    def test_anthropic_retries_reuse_prepared_headers(self):
        payload = {
            "model": "claude-review",
            "system": "Review only the supplied code.",
            "messages": [{"role": "user", "content": "Review"}],
        }
        sessions = []

        def requester(url, candidate, timeout, **kwargs):
            headers = kwargs.get("prepared_headers")
            if headers is None:
                headers = CLIENT.build_headers(
                    kwargs["protocol"],
                    kwargs["api_key"],
                    candidate,
                    simulated_client=kwargs["simulated_client"],
                )
            sessions.append(headers["X-Claude-Code-Session-Id"])
            if len(sessions) == 1:
                raise CLIENT.RetryableClientError("retry")
            return "review"

        with mock.patch.object(
            CLIENT.json, "loads", wraps=CLIENT.json.loads
        ) as loads:
            result = CLIENT._request_with_retries(
                requester,
                "https://review.test/v1/messages",
                payload,
                30,
                api_key="TEST_SECRET",
                protocol="anthropic",
                max_retries=1,
                simulated_client=True,
                sleeper=lambda seconds: None,
            )

        self.assertEqual(result, "review")
        self.assertEqual(loads.call_count, 1)
        self.assertEqual(len(set(sessions)), 1)

    def test_disabled_profile_does_no_preparation_work(self):
        self.assertIn(
            "simulated_client",
            inspect.signature(CLIENT._request_with_retries).parameters,
            "retry layer has no simulated client switch",
        )
        payload = {"model": "review", "input": "x" * 1_000_000}
        with (
            mock.patch.object(
                CLIENT, "_prepare_simulated_client_payload"
            ) as prepare,
            mock.patch.object(CLIENT.uuid, "uuid4") as new_uuid,
            mock.patch.object(CLIENT.secrets, "token_hex") as token_hex,
        ):
            result = CLIENT._request_with_retries(
                lambda *args, **kwargs: "review",
                "https://review.test/v1/responses",
                payload,
                30,
                api_key="TEST_SECRET",
                protocol="openai_responses",
                max_retries=0,
                simulated_client=False,
            )

        self.assertEqual(result, "review")
        prepare.assert_not_called()
        new_uuid.assert_not_called()
        token_hex.assert_not_called()
        self.assertNotIn("client_metadata", payload)

    def test_request_with_retries_reports_exhausted_attempts(self):
        attempts = 0

        def requester(*args, **kwargs):
            nonlocal attempts
            attempts += 1
            raise TimeoutError("upstream timed out")

        with self.assertRaisesRegex(CLIENT.ClientError, "after 3 attempts"):
            CLIENT._request_with_retries(
                requester,
                "https://review.test/v1/messages",
                {},
                30,
                api_key="TEST_SECRET",
                protocol="anthropic",
                max_retries=2,
                sleeper=lambda seconds: None,
            )

        self.assertEqual(attempts, 3)

    def test_request_with_retries_does_not_repeat_client_errors(self):
        attempts = 0

        def requester(*args, **kwargs):
            nonlocal attempts
            attempts += 1
            raise CLIENT.ClientError("invalid request")

        with self.assertRaisesRegex(CLIENT.ClientError, "invalid request"):
            CLIENT._request_with_retries(
                requester,
                "https://review.test/v1/messages",
                {},
                30,
                api_key="TEST_SECRET",
                protocol="anthropic",
                max_retries=3,
                sleeper=lambda seconds: None,
            )

        self.assertEqual(attempts, 1)

    def test_request_review_removes_terminal_control_characters(self):
        server = HTTPServer(("127.0.0.1", 0), ReviewHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{server.server_port}/control"

        try:
            result = CLIENT.request_review(
                url,
                {"model": "review-model"},
                timeout=5,
                api_key="TEST_SECRET",
            )
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()

        self.assertNotIn("\x1b", result)
        self.assertNotIn("\x07", result)
        self.assertNotIn("\r", result)
        self.assertNotIn("returned-secret", result)
        self.assertIn("[REDACTED]", result)
        self.assertIn("review result", result)

    def test_review_upstreams_runs_two_different_configs_concurrently_and_combines(self):
        configs = [
            CLIENT.Config(
                {
                    "protocol": "openai_chat",
                    "base_url": "http://one.test/chat",
                    "model": "model-one",
                    "api_key": "KEY_ONE",
                    "timeout_seconds": 700,
                }
            ),
            CLIENT.Config(
                {
                    "protocol": "anthropic",
                    "base_url": "https://two.test/messages",
                    "model": "model-two",
                    "api_key": "KEY_TWO",
                    "timeout_seconds": 800,
                }
            ),
        ]
        barrier = threading.Barrier(2)
        calls = []

        def requester(url, payload, timeout, **kwargs):
            calls.append((url, payload["model"], timeout, kwargs["protocol"]))
            barrier.wait(timeout=2)
            return f"review from {payload['model']}"

        combined, successes = CLIENT.review_upstreams(
            configs,
            "Review this change",
            [("src/app.py", "print('ok')")],
            requester=requester,
        )

        self.assertEqual(successes, 2)
        self.assertIn("Upstream 1 (openai_chat / model-one)", combined)
        self.assertIn("review from model-one", combined)
        self.assertIn("Upstream 2 (anthropic / model-two)", combined)
        self.assertIn("review from model-two", combined)
        self.assertCountEqual(
            calls,
            [
                (
                    "http://one.test/chat/v1/chat/completions",
                    "model-one",
                    700,
                    "openai_chat",
                ),
                (
                    "https://two.test/messages/v1/messages",
                    "model-two",
                    800,
                    "anthropic",
                ),
            ],
        )

    def test_review_upstreams_share_one_immutable_prompt_between_workers(self):
        configs = [
            CLIENT.Config({"base_url": "https://one.test", "api_key": "KEY_ONE"}),
            CLIENT.Config({"base_url": "https://two.test", "api_key": "KEY_TWO"}),
        ]
        barrier = threading.Barrier(2)
        prompt_ids = []
        prompt_texts = []

        def requester(url, payload, timeout, **kwargs):
            prompt = payload["messages"][1]["content"]
            prompt_ids.append(id(prompt))
            prompt_texts.append(prompt)
            barrier.wait(timeout=2)
            return "review"

        combined, successes = CLIENT.review_upstreams(
            configs,
            "Review",
            [("large.py", "x" * 1_000_000)],
            requester=requester,
        )

        self.assertEqual(successes, 2)
        self.assertEqual(combined.count("\n\nreview"), 2)
        self.assertEqual(len(set(prompt_texts)), 1)
        self.assertEqual(len(set(prompt_ids)), 1)

    def test_review_upstreams_removes_terminal_controls_from_model_heading(self):
        config = CLIENT.Config(
            {
                "base_url": "https://one.test",
                "model": "\x1b[31mreview-model\x07",
                "api_key": "KEY_ONE",
            }
        )

        combined, successes = CLIENT.review_upstreams(
            [config],
            "Review",
            [],
            requester=lambda *args, **kwargs: "review result",
        )

        self.assertEqual(successes, 1)
        self.assertNotIn("\x1b", combined)
        self.assertNotIn("\x07", combined)
        self.assertIn("review-model", combined)

    def test_review_upstreams_retries_each_upstream_independently_and_waits_for_both(self):
        configs = [
            CLIENT.Config(
                {
                    "base_url": "http://one.test",
                    "api_key": "KEY_ONE",
                    "max_retries": 3,
                }
            ),
            CLIENT.Config(
                {
                    "base_url": "https://two.test",
                    "api_key": "KEY_TWO",
                    "max_retries": 3,
                }
            ),
        ]
        attempts = {"one": 0, "two": 0}
        sleeps = []

        def requester(url, payload, timeout, **kwargs):
            name = "one" if url.startswith("http://one.test") else "two"
            attempts[name] += 1
            if name == "two" and attempts[name] <= 3:
                raise CLIENT.RetryableClientError("temporary second-upstream failure")
            return f"{name} review"

        combined, successes = CLIENT.review_upstreams(
            configs,
            "Review",
            [],
            requester=requester,
            sleeper=sleeps.append,
        )

        self.assertEqual(successes, 2)
        self.assertEqual(attempts, {"one": 1, "two": 4})
        self.assertEqual(sleeps, [1, 2, 4])
        self.assertIn("one review", combined)
        self.assertIn("two review", combined)

    def test_review_upstreams_keeps_success_when_another_upstream_fails(self):
        configs = [
            CLIENT.Config({"base_url": "http://one.test", "api_key": "KEY_ONE"}),
            CLIENT.Config({"base_url": "https://two.test", "api_key": "KEY_TWO"}),
        ]

        def requester(url, payload, timeout, **kwargs):
            if url.startswith("http://one.test"):
                raise TimeoutError("first upstream timed out")
            return "second upstream review"

        combined, successes = CLIENT.review_upstreams(
            configs,
            "Review",
            [],
            requester=requester,
            sleeper=lambda seconds: None,
        )

        self.assertEqual(successes, 1)
        self.assertIn("Upstream 1", combined)
        self.assertIn("Error: first upstream timed out", combined)
        self.assertIn("Upstream 2", combined)
        self.assertIn("second upstream review", combined)

    def test_review_upstreams_combines_two_timeout_errors(self):
        configs = [
            CLIENT.Config({"base_url": "http://one.test", "api_key": "KEY_ONE"}),
            CLIENT.Config({"base_url": "https://two.test", "api_key": "KEY_TWO"}),
        ]

        def requester(url, payload, timeout, **kwargs):
            raise TimeoutError(f"timeout from {payload['model']}")

        combined, successes = CLIENT.review_upstreams(
            configs,
            "Review",
            [],
            requester=requester,
            sleeper=lambda seconds: None,
        )

        self.assertEqual(successes, 0)
        self.assertEqual(combined.count("## Upstream"), 2)
        self.assertEqual(combined.count("Error: timeout from codereview"), 2)

    def test_parse_response_supports_three_protocols(self):
        self.assertEqual(
            CLIENT.parse_response(
                {"choices": [{"message": {"content": "chat"}}]},
                "openai_chat",
            ),
            "chat",
        )
        self.assertEqual(
            CLIENT.parse_response(
                {
                    "output": [
                        {"type": "reasoning", "content": []},
                        {
                            "type": "message",
                            "content": [{"type": "output_text", "text": "responses"}],
                        },
                    ]
                },
                "openai_responses",
            ),
            "responses",
        )
        self.assertEqual(
            CLIENT.parse_response(
                {"content": [{"type": "text", "text": "claude"}]},
                "anthropic",
            ),
            "claude",
        )

        with self.assertRaisesRegex(CLIENT.ClientError, "no usable text"):
            CLIENT.parse_response({"choices": []}, "openai_chat")

    def test_parse_response_includes_visible_reasoning_for_three_protocols(self):
        cases = [
            (
                "openai_chat",
                {
                    "choices": [
                        {
                            "message": {
                                "reasoning_content": "chat reasoning",
                                "content": "chat review",
                            }
                        }
                    ]
                },
                "chat reasoning",
                "chat review",
            ),
            (
                "openai_responses",
                {
                    "output": [
                        {
                            "type": "reasoning",
                            "summary": [
                                {"type": "summary_text", "text": "responses summary"}
                            ],
                        },
                        {
                            "type": "message",
                            "content": [
                                {"type": "output_text", "text": "responses review"}
                            ],
                        },
                    ]
                },
                "responses summary",
                "responses review",
            ),
            (
                "anthropic",
                {
                    "content": [
                        {"type": "thinking", "thinking": "claude thinking"},
                        {"type": "text", "text": "claude review"},
                    ]
                },
                "claude thinking",
                "claude review",
            ),
        ]

        for protocol, response, reasoning, review in cases:
            with self.subTest(protocol=protocol):
                result = CLIENT.parse_response(response, protocol)
                self.assertIn(
                    f"{{Upstream reasoning or summary ({protocol}):\n{reasoning}\n}}",
                    result,
                )
                self.assertIn(f"Review result:\n{review}", result)

    def test_parse_response_extracts_thinking_tags_from_review_text(self):
        result = CLIENT.parse_response(
            {
                "choices": [
                    {
                        "message": {
                            "content": "<think>tagged reasoning</think>\nactual review"
                        }
                    }
                ]
            },
            "openai_chat",
        )

        self.assertIn("tagged reasoning", result)
        self.assertIn("Review result:\nactual review", result)
        self.assertNotIn("<think>", result)

    def test_parse_response_extracts_non_ascii_tagged_reasoning_exactly(self):
        result = CLIENT.parse_response(
            {
                "choices": [
                    {"message": {"content": "<think>İ</think>\nactual review"}}
                ]
            },
            "openai_chat",
        )

        self.assertIn("\nİ\n}", result)
        self.assertNotIn("İ<", result)

    def test_parse_response_preserves_literal_thinking_tags_in_review_text(self):
        review = "Finding: code contains <think>literal</think> tag"

        result = CLIENT.parse_response(
            {"choices": [{"message": {"content": review}}]},
            "openai_chat",
        )

        self.assertEqual(result, review)

    def test_parse_response_accepts_reasoning_without_review_text(self):
        result = CLIENT.parse_response(
            {"choices": [{"message": {"reasoning": "reasoning only"}}]},
            "openai_chat",
        )

        self.assertIn("\nreasoning only\n}", result)
        self.assertIn("Review result:\n[No review text returned]", result)

    def test_parse_response_accepts_pure_thinking_wrapper_without_review_text(self):
        result = CLIENT.parse_response(
            {"choices": [{"message": {"content": "<think>reasoning only</think>"}}]},
            "openai_chat",
        )

        self.assertIn("\nreasoning only\n}", result)
        self.assertIn("Review result:\n[No review text returned]", result)

    def test_parse_stream_response_accepts_reasoning_without_review_text(self):
        stream = (
            'data: {"type":"response.reasoning_summary_text.delta","delta":"summary only"}\n\n'
            'data: [DONE]\n\n'
        )

        result = CLIENT.parse_stream_response(stream, "openai_responses")

        self.assertIn("\nsummary only\n}", result)
        self.assertIn("Review result:\n[No review text returned]", result)

    def test_parse_stream_response_preserves_redacted_error_details(self):
        cases = [
            (
                "openai_responses",
                'data: {"type":"error","error":{"type":"server_error","message":"upstream failed"}}\n\n',
                "server_error: upstream failed",
            ),
            (
                "anthropic",
                'event: error\ndata: {"type":"error","error":{"type":"overloaded_error","message":"service overloaded"}}\n\n',
                "overloaded_error: service overloaded",
            ),
        ]

        for protocol, stream, expected in cases:
            with self.subTest(protocol=protocol):
                with self.assertRaisesRegex(CLIENT.ClientError, expected):
                    CLIENT.parse_stream_response(stream, protocol)

    def test_parse_stream_response_includes_reasoning_for_three_protocols(self):
        cases = [
            (
                "openai_chat",
                'data: {"choices":[{"delta":{"reasoning":"chat reasoning"}}]}\n\n'
                'data: {"choices":[{"delta":{"content":"chat review"}}]}\n\n',
                "chat reasoning",
                "chat review",
            ),
            (
                "openai_responses",
                'data: {"type":"response.reasoning_summary_text.delta","delta":"responses summary"}\n\n'
                'data: {"type":"response.output_text.delta","delta":"responses review"}\n\n',
                "responses summary",
                "responses review",
            ),
            (
                "anthropic",
                'data: {"delta":{"type":"thinking_delta","thinking":"claude thinking"}}\n\n'
                'data: {"delta":{"type":"text_delta","text":"claude review"}}\n\n',
                "claude thinking",
                "claude review",
            ),
        ]

        for protocol, stream, reasoning, review in cases:
            with self.subTest(protocol=protocol):
                result = CLIENT.parse_stream_response(stream, protocol)
                self.assertIn(
                    f"{{Upstream reasoning or summary ({protocol}):\n{reasoning}\n}}",
                    result,
                )
                self.assertIn(f"Review result:\n{review}", result)

    def test_parse_stream_response_joins_reasoning_character_deltas_exactly(self):
        stream = (
            'data: {"choices":[{"delta":{"reasoning":"rea"}}]}\n\n'
            'data: {"choices":[{"delta":{"reasoning":"son ing"}}]}\n\n'
            'data: {"choices":[{"delta":{"content":"review"}}]}\n\n'
        )

        result = CLIENT.parse_stream_response(stream, "openai_chat")

        self.assertIn("\nreason ing\n}", result)
        self.assertNotIn("rea\nson ing", result)

    def test_parse_stream_response_preserves_list_delta_whitespace(self):
        stream = (
            'data: {"choices":[{"delta":{"reasoning":[{"text":"rea "}]}}]}\n\n'
            'data: {"choices":[{"delta":{"reasoning":[{"text":"son"}]}}]}\n\n'
            'data: {"choices":[{"delta":{"content":"review"}}]}\n\n'
        )

        result = CLIENT.parse_stream_response(stream, "openai_chat")

        self.assertIn("\nrea son\n}", result)

    def test_parse_stream_response_uses_completed_response_after_reasoning_delta(self):
        stream = (
            'data: {"type":"response.reasoning_summary_text.delta","delta":"summary"}\n\n'
            'data: {"type":"response.completed","response":{"output":[{"type":"message","content":[{"type":"output_text","text":"review"}]}]}}\n\n'
        )

        result = CLIENT.parse_stream_response(stream, "openai_responses")

        self.assertIn("\nsummary\n}", result)
        self.assertIn("Review result:\nreview", result)

    def test_stream_metadata_after_completed_cannot_hide_review_text(self):
        stream = (
            'data: {"type":"response.reasoning_summary_text.delta","delta":"summary"}\n\n'
            'data: {"type":"response.completed","response":{"output":[{"type":"message","content":[{"type":"output_text","text":"review"}]}]}}\n\n'
            'data: {"type":"response.rate_limits.updated","rate_limits":[]}\n\n'
        )

        result = CLIENT.parse_stream_response(stream, "openai_responses")

        self.assertIn("\nsummary\n}", result)
        self.assertIn("Review result:\nreview", result)

    def test_completed_reasoning_replaces_duplicate_streamed_reasoning(self):
        stream = (
            'data: {"type":"response.reasoning_summary_text.delta","delta":"summary"}\n\n'
            'data: {"type":"response.completed","response":{"output":['
            '{"type":"reasoning","summary":[{"type":"summary_text","text":"summary"}]},'
            '{"type":"message","content":[{"type":"output_text","text":"review"}]}'
            ']}}\n\n'
        )

        result = CLIENT.parse_stream_response(stream, "openai_responses")

        self.assertEqual(result.count("summary"), 2)
        self.assertIn("Review result:\nreview", result)

    def test_completed_reasoning_is_merged_with_streamed_review_text(self):
        stream = (
            'data: {"type":"response.output_text.delta","delta":"streamed review"}\n\n'
            'data: {"type":"response.completed","response":{"output":['
            '{"type":"reasoning","summary":[{"type":"summary_text","text":"final summary"}]},'
            '{"type":"message","content":[{"type":"output_text","text":"final review"}]}'
            ']}}\n\n'
        )

        result = CLIENT.parse_stream_response(stream, "openai_responses")

        self.assertIn("\nfinal summary\n}", result)
        self.assertIn("Review result:\nstreamed review", result)
        self.assertNotIn("final review", result)

    def test_in_progress_reasoning_cannot_override_streamed_reasoning(self):
        stream = (
            'data: {"type":"response.reasoning_summary_text.delta","delta":"stream-complete"}\n\n'
            'data: {"type":"response.output_text.delta","delta":"streamed review"}\n\n'
            'data: {"type":"response.in_progress","response":{"output":['
            '{"type":"reasoning","summary":[{"type":"summary_text","text":"partial-snapshot"}]}'
            ']}}\n\n'
            'data: {"type":"response.completed","response":{"output":['
            '{"type":"message","content":[{"type":"output_text","text":"final review"}]}'
            ']}}\n\n'
        )

        result = CLIENT.parse_stream_response(stream, "openai_responses")

        self.assertIn("\nstream-complete\n}", result)
        self.assertNotIn("partial-snapshot", result)
        self.assertIn("Review result:\nstreamed review", result)

    def test_completed_review_prefix_cannot_hide_streamed_reasoning(self):
        review = "{Upstream reasoning or summary (literal review text)"
        stream = (
            'data: {"type":"response.reasoning_summary_text.delta","delta":"summary"}\n\n'
            + "data: "
            + json.dumps(
                {
                    "type": "response.completed",
                    "response": {
                        "output": [
                            {
                                "type": "message",
                                "content": [{"type": "output_text", "text": review}],
                            }
                        ]
                    },
                }
            )
            + "\n\n"
        )

        result = CLIENT.parse_stream_response(stream, "openai_responses")

        self.assertIn("\nsummary\n}", result)
        self.assertIn(f"Review result:\n{review}", result)

    def test_parse_stream_response_does_not_retain_all_delta_events(self):
        event = 'data: {"choices":[{"delta":{"content":"0123456789abcdef0123456789abcdef"}}]}\n\n'
        text = event * 60000

        tracemalloc.start()
        try:
            result = CLIENT.parse_stream_response(text, "openai_chat")
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()

        self.assertEqual(len(result), 32 * 60000)
        self.assertLess(peak, 25_000_000)

    def test_redact_url_for_output_removes_sensitive_components(self):
        redacted = CLIENT.redact_url_for_output(
            "https://user:SECRET@example.test/messages?token=SECRET#fragment"
        )

        self.assertEqual(redacted, "https://example.test/messages")

    def test_build_doctor_report_is_redacted(self):
        config = CLIENT.Config(
            {
                "protocol": "anthropic",
                "base_url": "https://example.test/messages?tenant=private",
                "model": "review-model",
                "api_key": "TEST_SECRET",
            }
        )

        with mock.patch.dict(os.environ, {CLIENT.API_KEY_ENV: "TEST_SECRET"}, clear=True):
            report = CLIENT.build_doctor_report(
                [config],
                ignored_upstreams=0,
                active_source="environment",
                config_files=[],
            )

        encoded = json.dumps(report)
        self.assertEqual(report["active_config_source"], "environment")
        self.assertTrue(report["upstreams"][0]["api_key_present"])
        self.assertEqual(
            report["upstreams"][0]["base_url"],
            "https://example.test/messages",
        )
        self.assertEqual(
            report["upstreams"][0]["request_url"],
            "https://example.test/messages/v1/messages",
        )
        self.assertEqual(report["environment"][CLIENT.API_KEY_ENV], "present")
        self.assertEqual(report["upstreams"][0]["max_retries"], 3)
        self.assertNotIn("TEST_SECRET", encoded)
        self.assertNotIn("tenant", encoded)

    def test_main_doctor_does_not_require_question_or_call_upstream(self):
        values = {
            "protocol": "anthropic",
            "base_url": "https://example.test/messages",
            "model": "review-model",
            "api_key": "TEST_SECRET",
        }
        stdout = io.StringIO()
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch.object(CLIENT, "load_config_state", return_value=(values, "environment")),
            mock.patch.object(CLIENT, "config_file_statuses", return_value=[]),
            mock.patch.object(
                CLIENT,
                "review_upstreams",
                side_effect=AssertionError("doctor must not call upstreams"),
            ),
            mock.patch("sys.stdout", stdout),
        ):
            exit_code = CLIENT.main(["doctor"])

        report = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(report["enabled_upstream_count"], 1)
        self.assertEqual(report["upstreams"][0]["protocol"], "anthropic")

    def test_main_requires_question_outside_doctor(self):
        stderr = io.StringIO()
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch("sys.stderr", stderr),
        ):
            exit_code = CLIENT.main([])

        self.assertEqual(exit_code, 2)
        self.assertIn("--question is required", stderr.getvalue())

    def test_main_escapes_response_characters_unsupported_by_console_encoding(self):
        values = {
            "base_url": "https://example.test",
            "api_key": "TEST_SECRET",
        }
        raw_stdout = io.BytesIO()
        stdout = io.TextIOWrapper(raw_stdout, encoding="gbk")
        with (
            mock.patch.object(CLIENT, "load_config_state", return_value=(values, "test")),
            mock.patch.object(
                CLIENT,
                "review_upstreams",
                return_value=("review contains nonbreaking hyphen: \u2011", 1),
            ),
            mock.patch("sys.stdout", stdout),
        ):
            exit_code = CLIENT.main(["--question", "Review"])
            stdout.flush()

        self.assertEqual(exit_code, 0)
        self.assertIn(b"\\u2011", raw_stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
