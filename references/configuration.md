# Configuration

The bundled client uses only Python 3.11+ standard-library modules. Store real credentials in a user config file:

- Windows: `%USERPROFILE%\.config\third-party-code-review-skill\config.toml`
- macOS / Linux: `$HOME/.config/third-party-code-review-skill/config.toml`

Copy `config.example.toml` to the user path and fill each enabled table's `API_KEY`. The ignored skill-local `config.toml` is a lower-priority fallback for one machine.

## Source Priority

The first source containing any effective value is used as a unit; sources are not merged:

1. `%USERPROFILE%\.config\third-party-code-review-skill\config.toml`
2. `$HOME/.config/third-party-code-review-skill/config.toml`
3. Environment variables
4. Skill-local `config.toml`
5. The fallback `.toml` file named by `THIRD_PARTY_CODEREVIEW_CONFIG`
6. Client defaults

An active file with an empty `API_KEY` does not borrow a key from environment variables. There is no command-line API-key option. Invalid UTF-8, invalid TOML, unknown keys, invalid types, missing explicit files, and explicit fallback paths without a `.toml` suffix fail closed.

## Fixed Upstream Tables

TOML files may contain tables named `UPSTREAM<number>` or `UPSTREAM_<name>`. Enabled tables are evaluated in declaration order. The client sends to the first two enabled upstreams concurrently, ignores later enabled tables with a warning, and combines responses under `Upstream 1` and `Upstream 2` headings.

Each enabled table is independent and may use a different protocol, model, service root URL or gateway mount prefix, API key, timeout, retry count, and streaming mode. Each selected upstream has an independent retry counter; one may finish without retrying while another uses all configured retries. The client waits for both before combining results. One failed upstream is reported beside a successful result from the other. The process returns failure only when every selected upstream fails.

Each table may independently select the documented client identity profile; endpoint selection, authentication, retry policy, and response parsing continue to follow the upstream protocol. See [Client Simulation](#client-simulation) for the profile matrix.

```toml
[UPSTREAM_codereview]
ENABLED = true
PROTOCOL = "openai_chat"
BASE_URL = "http://172.28.100.252:10130/proxy/codereview_chat"
MODEL = "codereview"
API_KEY = ""
TIMEOUT_SECONDS = 600
MAX_RETRIES = 3
STREAM = true
SIMULATED_CLIENT = false

[UPSTREAM2]
ENABLED = true
PROTOCOL = "openai_responses"
BASE_URL = "https://api.example.test"
MODEL = "responses-review-model"
API_KEY = ""
TIMEOUT_SECONDS = 600
MAX_RETRIES = 3
STREAM = true
SIMULATED_CLIENT = false

[UPSTREAM3]
ENABLED = false
PROTOCOL = "anthropic"
BASE_URL = "https://gateway.example.test"
MODEL = "claude-review-model"
API_KEY = ""
TIMEOUT_SECONDS = 600
MAX_RETRIES = 3
STREAM = true
SIMULATED_CLIENT = false
```

Every enabled table must explicitly contain `PROTOCOL`, `BASE_URL`, `MODEL`, and `API_KEY`. Disabled tables may remain incomplete, but their key names and `ENABLED` type are still validated. Top-level scalar settings cannot be mixed with upstream tables.

## Fields

| TOML key | Default and constraint |
|---|---|
| `ENABLED` | Required table selector; boolean. Only the first two enabled tables are selected. |
| `PROTOCOL` | `openai_chat`; allowed: `openai_chat`, `openai_responses`, `anthropic`. |
| `BASE_URL` | `http(s)` service root URL or gateway mount prefix. It must not include the protocol endpoint path. User information and fragments are rejected; a query is allowed and preserved. |
| `MODEL` | Non-empty model identifier accepted by that upstream. |
| `API_KEY` | Required before a request; kept out of request bodies and normal output. |
| `TIMEOUT_SECONDS` | `600`; positive per-upstream total response deadline. Concurrent requests make total wait approximate the slowest selected upstream rather than the sum. |
| `MAX_RETRIES` | `3`; integer from `0` through `10`. This is the number of retries after the initial request, so the default permits up to four attempts per upstream. Each upstream counts independently. |
| `STREAM` | `true`; requests SSE by default. Set `false` for one JSON response. |
| `SIMULATED_CLIENT` | `false`; boolean. Enables the profile fixed for the selected protocol. `openai_chat` with `true` is a configuration error. |

`BASE_URL` may include a gateway mount prefix such as `/proxy/codereview_chat`, but it must not include the protocol endpoint path. The client removes a trailing slash from the configured path and appends `/v1/chat/completions` for `openai_chat`, `/v1/responses` for `openai_responses`, or `/v1/messages` for `anthropic`. A configured query is preserved on the resolved request URL. Existing values that already contain an endpoint are not detected or trimmed automatically and would produce a duplicated path.

## Environment Variables

Environment variables configure one upstream only. Use fixed TOML tables for concurrent multi-upstream review.

| TOML key | Environment variable |
|---|---|
| `PROTOCOL` | `THIRD_PARTY_CODEREVIEW_PROTOCOL` |
| `BASE_URL` | `THIRD_PARTY_CODEREVIEW_BASE_URL` |
| `MODEL` | `THIRD_PARTY_CODEREVIEW_MODEL` |
| `API_KEY` | `THIRD_PARTY_CODEREVIEW_API_KEY` |
| `TIMEOUT_SECONDS` | `THIRD_PARTY_CODEREVIEW_TIMEOUT_SECONDS` |
| `MAX_RETRIES` | `THIRD_PARTY_CODEREVIEW_MAX_RETRIES` |
| `STREAM` | `THIRD_PARTY_CODEREVIEW_STREAM` |

`THIRD_PARTY_CODEREVIEW_CONFIG` selects a lowest-priority fallback TOML file and is not a TOML key.

`SIMULATED_CLIENT` has no environment-variable or command-line override. Configure it in each TOML upstream table so the opt-in remains explicit and local to that upstream.

Command-line options that override a request URL, protocol, or model require exactly one enabled upstream. `--timeout`, `--stream`, and `--no-stream` may override common settings for every selected upstream. Retry count has no command-line override; configure `MAX_RETRIES` in the selected source.

## Doctor

Run `python scripts/codereview_client.py doctor` to inspect the effective configuration. The command reports `active_config_source`, candidate config files and their `exists` flags, environment variables as present or absent, selected and ignored upstream counts, and redacted upstream summaries including configured `base_url`, resolved `request_url`, `max_retries`, and `"simulated_client"`. API-key values are never emitted, and URL user information, query strings, and fragments are removed from displayed URLs. The command validates configuration but does not perform a network request.

## Protocol Selection

| Protocol | Request | Response text | Authentication |
|---|---|---|---|
| `openai_chat` | `model`, `messages`, optional `stream` | JSON message content or Chat SSE deltas | `Authorization: Bearer ...` |
| `openai_responses` | `model`, `instructions`, `input`, `store: false`, optional `stream` | JSON output text or Responses SSE deltas | `Authorization: Bearer ...` |
| `anthropic` | Anthropic-compatible Messages: `model`, top-level `system`, `messages`, optional `stream` | JSON text blocks or Anthropic SSE text deltas | `x-api-key` and a client-defined `anthropic-version` header |

## Client Simulation

`SIMULATED_CLIENT` is disabled by default. openai_chat does not currently support client simulation and fails during configuration when the switch is `true`.

| Protocol | Disabled profile | Enabled profile |
|---|---|---|
| `openai_chat` | Generic `third-party-code-review-skill/1.0` client | Unsupported |
| `openai_responses` | Generic client | Codex CLI 0.144.4 |
| `anthropic` | Generic client | Claude Code 2.1.210 |

The selected profile applies to streaming and non-streaming requests. It preserves the configured URL, request mode, retry policy, and authentication method. Simulation adds no `max_tokens`, output-length, temperature, or `tools` field.

### Codex CLI profile

- Sets `User-Agent: codex-tui/0.144.4 (Windows 10.0.19045; x86_64) WindowsTerminal (codex-tui; 0.144.4)`, `Version: 0.144.4`, `originator: codex-tui`, and `OpenAI-Beta: responses=experimental`.
- Sets `Content-Type: application/json`. `Accept` is `text/event-stream` for streaming requests and `application/json` for non-streaming requests.
- Generates installation, session, thread, turn, and window UUIDs. It exposes the matching values through `X-Codex-Installation-Id`, `Session-Id`, `Thread-Id`, `x-client-request-id`, `X-Codex-Window-Id`, and `X-Codex-Turn-Metadata`.
- Adds matching `client_metadata` to the Responses payload. The serialized turn metadata includes the same identifiers and `request_kind: turn`.
- Keeps `Authorization: Bearer ...` authentication. One upstream review reuses the same prepared identity and payload across retries.

### Claude Code profile

- Sets `User-Agent: claude-cli/2.1.210 (external, cli)`, `X-App: cli`, `anthropic-version: 2023-06-01`, `Accept: application/json`, and `Content-Type: application/json`.
- Sets `anthropic-beta` to `claude-code-20250219`, `interleaved-thinking-2025-05-14`, `redact-thinking-2026-02-12`, `context-management-2025-06-27`, `prompt-caching-scope-2026-01-05`, `mid-conversation-system-2026-04-07`, and `effort-2025-11-24`.
- Sets `Anthropic-Dangerous-Direct-Browser-Access: true` and the Stainless identity: JavaScript, package `0.94.0`, Linux, arm64, Node `v24.3.0`, retry count `0`, and timeout `600`.
- Generates a stable session UUID for `X-Claude-Code-Session-Id` and JSON-string `metadata.user_id`. The payload identity also contains a random 64-character hexadecimal `device_id` and an empty `account_uuid`.
- Prepends an ephemeral system block identifying Claude Code before the existing review policy, while keeping `x-api-key` authentication unchanged.

The normal retry path prepares the payload and header set in place once before its first attempt, then reuses both. It does not deep-copy the payload, duplicate large attachment strings, cache serialized request bodies, or retain global session state. With the switch disabled, profile parsing and random identity generation are skipped.

When `STREAM` is omitted, the client sends `stream: true`. Set `STREAM = false` or use `--no-stream` for one JSON response. The client still parses the actual response as SSE or JSON without a second request.

Visible reasoning is parsed without adding request parameters. `openai_chat` accepts `reasoning`, `reasoning_content`, or `thinking` message and delta fields; `openai_responses` accepts reasoning summary items and reasoning summary/text delta events; `anthropic` accepts `thinking` blocks and `thinking_delta`. A single complete leading `<think>` or `<thinking>` wrapper is separated as reasoning even when no review text follows it; literal tags elsewhere remain review text. Signatures and encrypted thinking metadata are ignored.

When reasoning exists, output places `{Upstream reasoning or summary (<protocol>): ...}` before `Review result`. Without reasoning, output remains the plain review text. Reasoning-only output is valid and marks the body `[No review text returned]`; only a response containing neither reasoning nor review text is empty and retryable.

Each upstream may make up to `1 + MAX_RETRIES` stateless attempts. Retryable failures are transport errors, timeouts, HTTP 408/429/5xx, invalid UTF-8, invalid JSON/SSE, and responses with no usable text. Redirects, other 4xx responses, configuration errors, and local input errors fail immediately. Retry waits are 1, 2, and 4 seconds, then remain capped at 8 seconds. With two upstreams at the default, the maximum is eight external attempts in total, but the counters and completion timing remain independent.

Requests use `User-Agent: third-party-code-review-skill/1.0` unless a supported upstream explicitly enables the profile above.

The client does not send model context or output-length parameters for any protocol. Upstream defaults and limits therefore govern both. Raw response capture accepts up to and including `10000000` bytes per upstream attempt and rejects the next byte for resource safety. `anthropic` targets Anthropic-compatible Messages gateways that accept omitted `max_tokens`; it does not claim direct compatibility with the official Anthropic endpoint, where `max_tokens` is normally required. The `anthropic-version` header uses a client-internal compatibility value and is not configurable.

## Tool Policy

Requests contain no `tools` field, and the client provides no local Web, Shell, MCP, file-search, function-calling, code-execution, or filesystem bridge. It only converts the explicitly approved scope to screened text. A custom upstream may still have server-side capabilities configured by its operator; verify that service separately.

Any future tool support requires a separate opt-in design with a per-tool allowlist, argument validation, human approval, bounded iterations, and an auditable result path.

## Safety

- OpenAI protocols place the key in `Authorization: Bearer ...`; Anthropic-compatible Messages uses `x-api-key`. Keys never enter JSON request bodies or normal output.
- Each selected upstream uses only the key from its own table. Empty selected-file keys never borrow environment-variable keys.
- Responses requests set `store: false`.
- Redirects are refused so authentication headers remain on the configured endpoint.
- Git diff collection uses NUL-delimited path inspection, rejects blocked paths, and disables rename detection, external diff drivers, and text conversion.
- Files, Git output, and API responses are read in bounded chunks. Oversized input is rejected during capture instead of after an unbounded read.
- The configured HTTP timeout is tracked with a monotonic total deadline while the response body is read; responses of at most `10000000` bytes are accepted.
- Successful and error responses have terminal control characters removed and common secret-like values redacted before output.
- Characters unsupported by the active console encoding are emitted as backslash escapes so arbitrary upstream Unicode cannot terminate local output.
- Plain HTTP is allowed for trusted configured endpoints, including internal relays and domains that local DNS or Clash resolves to internal addresses. The client does not block by resolved IP and warns for each plain-HTTP upstream.
- Treat every returned review as untrusted advisory data and verify it locally before adoption.

## Protocol References

- OpenAI Chat Completions: <https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create>
- OpenAI Responses migration and response shape: <https://developers.openai.com/api/docs/guides/migrate-to-responses>
- Anthropic Messages schema reference (`max_tokens` is required by the official endpoint): <https://platform.claude.com/docs/en/api/messages/create>
- OpenAI Python `base_url`: <https://github.com/openai/openai-python/blob/main/src/openai/_client.py>
- Anthropic Python `base_url`: <https://github.com/anthropics/anthropic-sdk-python/blob/main/src/anthropic/_client.py>
- Least-privilege tool guidance: <https://cheatsheetseries.owasp.org/cheatsheets/AI_Agent_Security_Cheat_Sheet.html>
