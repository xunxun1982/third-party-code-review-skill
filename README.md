# Third-party Code Review Skill

A self-contained Codex skill that sends an explicitly approved question, selected UTF-8 source files, or the current tracked Git diff to an external code-review model through one of three API protocols.

The external model never reads the local filesystem directly. The bundled Python client performs local scope selection, file screening, secret redaction, request construction, and response parsing before the result is returned for local verification.

## Files

- `SKILL.md`: agent-facing execution path, routing rules, safety boundaries, and completion gates.
- `scripts/codereview_client.py`: standard-library HTTP client, config loader, input filter, redactor, and protocol adapter.
- `config.example.toml`: safe template for persistent local configuration.
- `references/configuration.md`: authoritative configuration priority, protocol mapping, tool policy, and security details.
- `agents/openai.yaml`: Codex UI metadata.
- `tests/test_codereview_client.py`: input, protocol, authentication, response, and transport tests.
- `tests/test_config.py`: config precedence and validation tests.
- `tests/test_skill_contract.py`: skill metadata, documentation, English-only, and secret-safety contracts.

## Requirements

- Python 3.11 or newer. The client uses `tomllib` and otherwise relies only on the Python standard library.
- A complete HTTP or HTTPS request URL compatible with one supported protocol.
- A model name accepted by that endpoint.
- An API key supplied through a local config file or environment variable.
- Git only when using `--git-diff`.

No package installation or virtual environment is required.

## Configuration

Copy `config.example.toml` to the platform user config path:

- Windows: `%USERPROFILE%\.config\third-party-code-review-skill\config.toml`
- macOS / Linux: `$HOME/.config/third-party-code-review-skill/config.toml`

Configure one or more fixed upstream tables. Table names must use `UPSTREAM<number>` or `UPSTREAM_<name>`:

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
RETURN_REASONING = true
SIMULATED_CLIENT = false
```

Enabled tables are evaluated in declaration order. The client sends the same screened scope to the first two enabled upstreams concurrently, ignores later enabled tables with a warning, and combines responses under `Upstream 1` and `Upstream 2` headings. Each selected table may use a different protocol, model, URL, timeout, API key, retry count, streaming mode, and reasoning-output mode. Each upstream has an independent retry counter: one may succeed without retrying while the other uses its configured retries. The client waits for both before combining results. One failed upstream does not discard a successful result from the other; the command fails only when every selected upstream fails.

`RETURN_REASONING` is a per-upstream TOML boolean and defaults to `true` for compatibility. It controls only whether visible reasoning or summary fields are included in local output; it does not add a reasoning request field or change model generation. Set it to `false` to return only review text. A reasoning-only response then returns `[No review text returned]` without retrying. There is no environment-variable or command-line override.

`SIMULATED_CLIENT` is a per-upstream TOML boolean and is disabled by default. There is no environment-variable or command-line override. openai_chat does not currently support client simulation and fails configuration when the switch is `true`. An enabled profile is prepared once per upstream and reused across retries for streaming and non-streaming requests.

`MAX_RETRIES = 3` means three retries after the initial request, for up to four attempts per upstream. Valid values are `0` through `10`; `0` disables retries for that upstream. Retries wait 1, 2, and 4 seconds by default, with later waits capped at 8 seconds.

The client does not declare a model context window or output length. Each upstream applies its own supported limits and defaults. Local capture accepts up to and including `10000000` raw response bytes per upstream attempt and rejects the next byte for resource safety; that implementation guard is not a model setting or user configuration.

Do not commit a real API key. The repository ignores skill-local `config.toml`, but the user config path is preferred because it survives repository updates and stays outside version control.

The first source containing any effective value is used as a unit; sources are not merged:

1. `%USERPROFILE%\.config\third-party-code-review-skill\config.toml`.
2. `$HOME/.config/third-party-code-review-skill/config.toml`.
3. Environment variables.
4. Skill-local `config.toml`.
5. The fallback `.toml` file named by `THIRD_PARTY_CODEREVIEW_CONFIG`.
6. Built-in defaults.

Command-line options override the selected source for one call. Request URL, protocol, and model overrides require exactly one enabled upstream; timeout and stream overrides may apply to all selected upstreams. Keep all required service settings in the same source: an active file with an empty `API_KEY` does not borrow a key from environment variables.

Environment-variable configuration is also supported:

```powershell
$env:THIRD_PARTY_CODEREVIEW_PROTOCOL = "openai_chat"
$env:THIRD_PARTY_CODEREVIEW_BASE_URL = "http://127.0.0.1/review"
$env:THIRD_PARTY_CODEREVIEW_MODEL = "codereview"
$env:THIRD_PARTY_CODEREVIEW_API_KEY = ""
$env:THIRD_PARTY_CODEREVIEW_TIMEOUT_SECONDS = "600"
$env:THIRD_PARTY_CODEREVIEW_MAX_RETRIES = "3"
```

Environment variables describe one upstream only. Use TOML upstream tables for concurrent multi-upstream review.

The API key intentionally has no command-line option, which keeps it out of shell history and process arguments.

See [references/configuration.md](references/configuration.md) for every field, environment variable, validation rule, and precedence detail.

## Protocols

Set each upstream table's `PROTOCOL` to exactly one of these values:

| Protocol | `SIMULATED_CLIENT = false` | `SIMULATED_CLIENT = true` |
|---|---|---|
| `openai_chat` | Generic `third-party-code-review-skill/1.0` client | Unsupported; configuration fails |
| `openai_responses` | Generic client | Codex CLI 0.144.4 |
| `anthropic` | Generic client | Claude Code 2.1.210 |

| Value | Request shape | Authentication | Parsed response text |
|---|---|---|---|
| `openai_chat` | OpenAI Chat Completions `model` and `messages` | `Authorization: Bearer ...` | `choices[0].message.content` |
| `openai_responses` | OpenAI Responses `instructions`, `input`, and `store: false` | `Authorization: Bearer ...` | `output[].content[type=output_text].text` |
| `anthropic` | Anthropic-compatible Messages top-level `system` and `messages` | `x-api-key` and a client-defined `anthropic-version` header | `content[type=text].text` |

`BASE_URL` is the service root URL or a gateway mount prefix. It may include a path such as `/proxy/codereview_chat`, but it must not include the protocol endpoint path. The client appends `/v1/chat/completions`, `/v1/responses`, or `/v1/messages` according to `PROTOCOL`. A configured query is preserved on the final request URL.

When `RETURN_REASONING = true`, the client preserves visible upstream reasoning or summary data returned by the selected protocol. It does not add a reasoning request parameter. When reasoning is present, the output is:

```text
{Upstream reasoning or summary (<protocol>):
<reasoning or summary text>
}

Review result:
<review text>
```

Without reasoning, or with `RETURN_REASONING = false`, the plain review text is returned. Reasoning-only output is successful and uses `[No review text returned]`; when reasoning output is enabled, that placeholder appears under `Review result` so upstream withdrawals or omitted final answers remain visible.

For a gateway that exposes the complete route as its base, use:

```toml
BASE_URL = "https://gateway.example/direct-review"
```

For example, `openai_chat` resolves that value to `https://gateway.example/direct-review/v1/chat/completions`. Do not configure the resolved endpoint as `BASE_URL`, or the path would be appended twice.

OpenAI Responses requests set `store: false`. Streaming is enabled by default. `STREAM = false` requests one JSON response instead. Each mode uses one stateless HTTP request.

### OpenAI Chat Completions

Choose this mode for endpoints that implement the Chat Completions message schema, including the preconfigured private review proxy.

```toml
[UPSTREAM1]
ENABLED = true
PROTOCOL = "openai_chat"
BASE_URL = "http://172.28.100.252:10130/proxy/codereview_chat"
MODEL = "codereview"
API_KEY = ""
TIMEOUT_SECONDS = 600
MAX_RETRIES = 3
STREAM = true
RETURN_REASONING = true
SIMULATED_CLIENT = false
```

The client sends:

```json
{
  "model": "codereview",
  "messages": [
    {"role": "system", "content": "<review policy>"},
    {"role": "user", "content": "<question and screened attachments>"}
  ]
}
```

The key is sent as `Authorization: Bearer ...`. Text is read from `choices[0].message.content`. The compatibility payload intentionally omits output-token parameters because third-party Chat-compatible gateways differ on `max_tokens` and `max_completion_tokens` support.

Keep `SIMULATED_CLIENT = false` for this protocol. Client simulation is not currently implemented for OpenAI Chat Completions.

### OpenAI Responses

Choose this mode for endpoints that implement the OpenAI Responses shape.

```toml
[UPSTREAM2]
ENABLED = true
PROTOCOL = "openai_responses"
BASE_URL = "https://api.example.test"
MODEL = "responses-review-model"
API_KEY = ""
TIMEOUT_SECONDS = 600
MAX_RETRIES = 3
STREAM = true
RETURN_REASONING = true
SIMULATED_CLIENT = false
```

The client sends:

```json
{
  "model": "responses-review-model",
  "instructions": "<review policy>",
  "input": "<question and screened attachments>",
  "store": false
}
```

The key is sent as `Authorization: Bearer ...`. Text is collected from message output blocks whose type is `output_text`; an `output_text` helper string is also accepted for compatible proxies.

With `SIMULATED_CLIENT = true`, the request uses the pinned Codex CLI 0.144.4 TUI User-Agent, version and originator headers, Codex installation/session/thread/window/turn identity headers, and matching `client_metadata`. Streaming requests advertise SSE while non-streaming requests advertise JSON. Bearer authentication and the existing Responses fields remain unchanged; the profile adds no output-length, temperature, or tools field. See [references/configuration.md](references/configuration.md#client-simulation) for the complete observable profile.

### Anthropic-compatible Messages

Choose this mode for Anthropic-compatible gateways that implement the Messages shape and accept an upstream-defined output limit. It is not a direct compatibility promise for the official Anthropic endpoint, where `max_tokens` is normally required.

```toml
[UPSTREAM3]
ENABLED = true
PROTOCOL = "anthropic"
BASE_URL = "https://gateway.example.test"
MODEL = "claude-review-model"
API_KEY = ""
TIMEOUT_SECONDS = 600
MAX_RETRIES = 3
STREAM = true
RETURN_REASONING = true
SIMULATED_CLIENT = false
```

The client sends:

```json
{
  "model": "claude-review-model",
  "system": "<review policy>",
  "messages": [
    {"role": "user", "content": "<question and screened attachments>"}
  ]
}
```

The key is sent in `x-api-key`. The client supplies an internal compatibility value in the `anthropic-version` header; it is not configurable. The upstream must accept omitted `max_tokens` and provide its own output default. Text is joined from response content blocks whose type is `text`.

With `SIMULATED_CLIENT = true`, the request uses the pinned Claude Code 2.1.210 CLI User-Agent, Claude beta and Stainless headers, a stable session header, matching `metadata.user_id`, and an ephemeral Claude Code identity system block before the review policy. The same profile is used in both response modes. `x-api-key` authentication remains unchanged, and the profile adds no `max_tokens`, temperature, or tools field. See [references/configuration.md](references/configuration.md#client-simulation) for the complete observable profile.

## Usage

Resolve the skill directory that contains `SKILL.md`, set it as the shell working directory, and run the relative `scripts/codereview_client.py` entry point. Do not run the client from the repository being reviewed or hard-code where the skill is installed; pass target files with `--file` and target repositories with `--cwd`.

Show all options:

```bash
python scripts/codereview_client.py --help
```

Inspect the effective configuration without sending a request:

```bash
python scripts/codereview_client.py doctor
```

The redacted JSON reports `active_config_source`, candidate config files with `exists` flags, environment variables as present or absent, enabled and ignored upstream counts, and each selected upstream's protocol, model, sanitized `base_url`, resolved `request_url`, key-presence flag, `max_retries`, stream mode, `return_reasoning` mode, and `simulated_client` state. It never prints API-key values, removes URL user information, query strings, and fragments, and does not perform a network request.

Review selected files:

```bash
python scripts/codereview_client.py \
  --question "Review correctness, security, compatibility, and regression risk" \
  --cwd "/path/to/repository" \
  --file "src/app.py" \
  --file "tests/test_app.py"
```

PowerShell equivalent:

```powershell
python scripts/codereview_client.py `
  --question "Review correctness, security, compatibility, and regression risk" `
  --cwd "C:\path\to\repository" `
  --file "src/app.py" `
  --file "tests/test_app.py"
```

Review the current tracked diff relative to `HEAD`. Before the first commit,
this reviews staged files instead; run `git add` first. For a directory that is
not a Git work tree, select files explicitly with `--file`:

```bash
python scripts/codereview_client.py \
  --question "Review the current changes" \
  --git-diff \
  --cwd "/path/to/repository"
```

Send a question without local code:

```bash
python scripts/codereview_client.py --question "Which failure modes should this design test?"
```

Override non-secret settings for one call:

```bash
python scripts/codereview_client.py \
  --question "Review this file" \
  --cwd "/path/to/repository" \
  --file "src/app.py" \
  --protocol openai_responses \
  --base-url "https://example.test" \
  --model "review-model" \
  --timeout 600
```

The client runs at most two upstream tasks concurrently. Each task may make up to `1 + MAX_RETRIES` requests with its own independent counter; the default is up to four attempts per upstream and eight attempts across two upstreams. If the selected scope exceeds the internal capture ceiling, bounded capture stops and rejects the scope; reduce it and run a new explicitly chosen review instead of relying on automatic truncation or request chunking.

With client simulation disabled, every request sends `User-Agent: third-party-code-review-skill/1.0`. Enabling the supported per-upstream profile replaces that generic identifier with the pinned Codex CLI or Claude Code identity described under Protocols.

Streaming can be overridden for one call:

```bash
python scripts/codereview_client.py --question "Review this change" --stream
python scripts/codereview_client.py --question "Review this change" --no-stream
```

With neither flag and no `STREAM` setting, the request uses streaming. The client parses either JSON or SSE based on the actual response. For SSE, it returns as soon as the standard terminal signal is complete: `[DONE]` for OpenAI Chat, `response.completed` for OpenAI Responses, or `message_stop` for Anthropic. If a compatible stream omits its terminal signal, the existing EOF and total-deadline behavior remains in force.

## Security Model

### Explicit scope

The client reads only paths passed with `--file` or the tracked diff requested with `--git-diff`. Relative file paths are resolved from `--cwd`, so the client can remain in the skill directory while labels stay relative to the reviewed repository. It does not scan the repository for extra context, follow imports, add neighboring files, or include untracked files automatically.

### Blocked input

The client rejects:

- `.env*` files.
- `config.toml`, `auth.json`, and `credentials.json`.
- Private-key and certificate suffixes such as `.key`, `.pem`, `.p12`, `.pfx`, `.crt`, and `.cer`.
- Binary files detected by NUL bytes.
- Symbolic links.
- A tracked diff containing any blocked path.

Files must be valid UTF-8 text. A blocked item fails the request instead of being silently skipped.

### Redaction

Before transmission, the client redacts common secret forms:

- API-key, token, secret, password, and authorization assignments.
- OpenAI-style key strings.
- Bearer tokens.
- PEM private-key blocks.

Redaction is defense in depth, not a guarantee that every secret format can be recognized. Minimum disclosure and explicit file selection remain the primary controls.

Returned review text is screened again for terminal control characters and common secret-like values before display. Characters unsupported by the active console encoding are emitted as backslash escapes instead of terminating the command. This may redact credential-shaped examples in an otherwise valid finding.

### No external tools

Requests contain no `tools` field, and the client provides no local Web, Shell, MCP, file-search, function-calling, code-execution, or filesystem bridge. It sends only screened text. A custom upstream may still have server-side capabilities configured by its operator; this client cannot inspect or disable them.

This avoids an unnecessary agent loop and bounds prompt-injection and data disclosure to the explicitly selected upstreams and their configured retry limits. Future tool support would require a separate opt-in design with allowlists, argument validation, bounded iterations, approval, and audit logging.

### Transport and storage

Trusted configured endpoints may use plain HTTP, including internal relays and domains that local DNS or Clash resolves to internal addresses. The client does not reject HTTP based on hostname or resolved IP and prints a warning before every plain-HTTP request. Use HTTPS before the route crosses an untrusted network.

OpenAI Responses requests include `store: false`. Storage and training policies of custom proxies remain server-side concerns and must be verified with the service operator.

Redirects are refused so API credentials remain on the exact configured endpoint. `TIMEOUT_SECONDS` is enforced separately for every attempt as a monotonic total response deadline, and response capture accepts at most `10000000` bytes. A complete protocol terminal SSE event ends capture without waiting for the server to close the HTTP connection. Transport failures, timeouts, HTTP 408/429/5xx, invalid UTF-8, API responses containing HTML instead of the expected JSON or SSE format, invalid JSON/SSE, and responses with no usable text are retryable. Redirects, other 4xx responses, configuration errors, and local input errors are not retried.

### Untrusted response

The returned review is advisory data. Verify file paths, locations, trigger conditions, execution paths, and claimed impact against the local repository before adopting a finding or changing code.

## Testing

Run the complete standard-library test suite:

```bash
python -m unittest discover -s tests -v
```

Validate the skill structure with the Codex `skill-creator` validator available in the host installation:

```bash
python <skill-creator-directory>/scripts/quick_validate.py .
```

The tests cover:

- Three protocol payload shapes with no tools.
- Omitted, explicit streaming, explicit non-streaming, and three protocol-specific SSE formats.
- Complete Codex CLI and Claude Code profiles for streaming and non-streaming requests, stable retry identity, and a zero-preparation disabled path.
- Protocol-specific authentication headers.
- Chat, Responses, and Anthropic-compatible text extraction.
- Fixed and named upstream tables, disabled entries, and the first-two-enabled limit.
- Concurrent requests across different protocols, models, URLs, timeouts, and partial failures.
- Independent upstream retry counters, retry exhaustion, non-retryable failures, and 1/2/4-second backoff.
- Config defaults, precedence, invalid TOML, invalid types, and invalid ranges.
- Sensitive-file rejection, binary and symlink boundaries, bounded file and Git capture, redaction, and size limits.
- Git rename-detection disabling and total response-deadline enforcement.
- Local loopback HTTP behavior without calling the configured third-party service.
- UTF-8 skill metadata, required documentation, absence of Han-script text, and absence of committed keys.

## Limitations

- The client sends text only; it does not upload images, PDFs, archives, or binary artifacts.
- With `HEAD`, `--git-diff` includes tracked staged and unstaged changes relative to it. Before the first commit, it includes staged files only. It never includes untracked files automatically.
- The client does not calculate model tokens or send context-window or output-length settings. An internal `10000000`-byte raw-response guard protects local resources, while the upstream governs model limits.
- The client does not split or truncate. It retries only the documented transient failures, merges at most two independently returned reviews after both tasks finish, and buffers SSE under the same raw-response limit before each review is combined.
- Secret redaction is pattern-based and cannot replace human scope review.
- Compatible proxies vary in optional parameter support, so all protocol payloads omit output-length parameters. Official Anthropic Messages normally requires `max_tokens`; `anthropic` mode therefore requires an upstream that accepts its omission.
- Protocol selection controls request JSON, authentication, and response parsing. Optional client simulation also changes the documented headers and identity metadata; the client cannot verify that a custom endpoint recognizes either profile until a request is made.

## References

- [OpenAI Chat Completions API](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)
- [OpenAI Responses migration guide](https://developers.openai.com/api/docs/guides/migrate-to-responses)
- [OpenAI Python client `base_url` configuration](https://github.com/openai/openai-python/blob/main/src/openai/_client.py)
- [Anthropic Messages schema reference](https://platform.claude.com/docs/en/api/messages/create) (`max_tokens` is required by the official endpoint)
- [Anthropic Python client `base_url` configuration](https://github.com/anthropics/anthropic-sdk-python/blob/main/src/anthropic/_client.py)
- [OWASP AI Agent Security Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/AI_Agent_Security_Cheat_Sheet.html)
