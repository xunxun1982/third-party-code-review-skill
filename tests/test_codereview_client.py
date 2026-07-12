import importlib.util
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
