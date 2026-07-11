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

[UPSTREAM2]
ENABLED = true
PROTOCOL = "openai_responses"
BASE_URL = "https://api.example.test"
MODEL = "responses-review-model"
API_KEY = ""
TIMEOUT_SECONDS = 600
MAX_RETRIES = 3
STREAM = true

[UPSTREAM3]
ENABLED = false
PROTOCOL = "anthropic"
BASE_URL = "https://gateway.example.test"
MODEL = "claude-review-model"
API_KEY = ""
TIMEOUT_SECONDS = 600
MAX_RETRIES = 3
STREAM = true
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

Command-line options that override a request URL, protocol, or model require exactly one enabled upstream. `--timeout`, `--stream`, and `--no-stream` may override common settings for every selected upstream. Retry count has no command-line override; configure `MAX_RETRIES` in the selected source.

## Doctor

Run `python scripts/codereview_client.py doctor` to inspect the effective configuration. The command reports `active_config_source`, candidate config files and their `exists` flags, environment variables as present or absent, selected and ignored upstream counts, and redacted upstream summaries including both configured `base_url`, resolved `request_url`, and `max_retries`. API-key values are never emitted, and URL user information, query strings, and fragments are removed from displayed URLs. The command validates configuration but does not perform a network request.

## Protocol Selection

| Protocol | Request | Response text | Authentication |
|---|---|---|---|
| `openai_chat` | `model`, `messages`, optional `stream` | JSON message content or Chat SSE deltas | `Authorization: Bearer ...` |
| `openai_responses` | `model`, `instructions`, `input`, `store: false`, optional `stream` | JSON output text or Responses SSE deltas | `Authorization: Bearer ...` |
| `anthropic` | Anthropic-compatible Messages: `model`, top-level `system`, `messages`, optional `stream` | JSON text blocks or Anthropic SSE text deltas | `x-api-key` and a client-defined `anthropic-version` header |

When `STREAM` is omitted, the client sends `stream: true`. Set `STREAM = false` or use `--no-stream` for one JSON response. The client still parses the actual response as SSE or JSON without a second request.

Each upstream may make up to `1 + MAX_RETRIES` stateless attempts. Retryable failures are transport errors, timeouts, HTTP 408/429/5xx, invalid UTF-8, invalid JSON/SSE, and responses with no usable text. Redirects, other 4xx responses, configuration errors, and local input errors fail immediately. Retry waits are 1, 2, and 4 seconds, then remain capped at 8 seconds. With two upstreams at the default, the maximum is eight external attempts in total, but the counters and completion timing remain independent.

Every request sends `User-Agent: third-party-code-review-skill/1.0`. This identifies the client without impersonating a browser.

The client does not send model context or output-length parameters for any protocol. Upstream defaults and limits therefore govern both. `anthropic` targets Anthropic-compatible Messages gateways that accept omitted `max_tokens`; it does not claim direct compatibility with the official Anthropic endpoint, where `max_tokens` is normally required. The `anthropic-version` header uses a client-internal compatibility value and is not configurable.

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
- The configured HTTP timeout is tracked with a monotonic total deadline while the response body is read; response bytes remain capped at `1000000`.
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
