# Opt-in Responses transport

Odysseus keeps Chat Completions as the default for ordinary API endpoints.
Native Ollama, Anthropic, and ChatGPT subscription routing retain their existing
behavior. Protocol, reasoning, and tool support are separate model capabilities;
selecting Responses does not assert that a model supports every effort or tool.

## Configuration

Add `llm_protocol_overrides` to the existing `data/settings.json` (or the settings
store under `ODYSSEUS_DATA_DIR`). Each entry matches an exact `base_url` and exact
`model`; known `/chat/completions`, `/responses`, and `/models` suffixes and trailing
slashes are removed for matching. There is no model-prefix routing or global mode.
The first matching entry wins. Preserve the rest of the settings file.

For an OpenAI API endpoint already configured with its API key:

```json
{
  "llm_protocol_overrides": [
    {
      "base_url": "https://api.openai.com/v1",
      "model": "gpt-6-luna",
      "api_mode": "responses",
      "reasoning_effort": "medium"
    }
  ]
}
```

For Ollama's OpenAI-compatible endpoint (this explicit entry is optional):

```json
{
  "llm_protocol_overrides": [
    {
      "base_url": "http://localhost:11434/v1",
      "model": "local-model",
      "api_mode": "chat_completions"
    }
  ]
}
```

Both entries can coexist in the same list. LM Studio, vLLM, and compatible proxies
keep Chat Completions by default. To opt a compatible endpoint into Responses,
use its actual base URL and model in a Responses entry. The custom URL prefix,
authentication headers, connection timeout, read timeout, and local concurrency
gate are preserved. Verify Responses support with that server and selected model;
OpenAI compatibility alone does not establish it. Native Ollama `/api/chat` and
Anthropic `/v1/messages` remain separate adapters, not Chat Completions endpoints.

Supported `api_mode` values are `responses` and `chat_completions`. Omitted mode
retains the legacy route, except that a direct transport call to a URL ending in
`/responses` explicitly selects Responses. The endpoint resolver still builds
legacy chat URLs; settings overrides are resolved at the transport boundary.

Optional `reasoning_effort` maps to `reasoning.effort` in Responses and to
`reasoning_effort` in Chat Completions. Omitting it leaves the provider's default.
The adapter accepts `none`, `minimal`, `low`, `medium`, `high`, `xhigh`, and `max`,
but models support different subsets. Unsupported model/effort combinations
surface the provider error without retrying a validation failure, downgrading
reasoning, or switching models. Explicit legacy effort is never changed. Existing
unconfigured GPT-5 Chat Completions calls retain their historical tools/effort
workaround for backward compatibility; use Responses to retain reasoning.

OpenAI documents GPT-6 Luna efforts as `none`, `low`, `medium`, `high`, `xhigh`,
and `max`, with Responses supporting reasoning and function calling together.
See the [official model documentation](https://developers.openai.com/api/docs/models/gpt-6-luna).

To switch back, set the entry's mode to `chat_completions` or remove the entry.
There is no database or conversation migration. Source/image installations must
run the updated code before the setting takes effect; this change does not restart
or deploy the running service. Settings reads use the existing two-second cache.

## Developer contract

`llm_call`, `llm_call_async`, and `stream_llm` accept optional `api_mode` and
`reasoning_effort` keyword arguments, which override a matching settings entry.
The existing fallback wrappers forward those arguments. No fallback model is
introduced: existing explicitly configured availability fallbacks remain usable.

The repository uses `httpx` directly and has no installed OpenAI SDK dependency.
The implementation follows the documented HTTP contract for
`client.responses.create(...)` rather than adding an SDK solely for this adapter.
Chat Completions and the specialized subscription adapter keep their existing
HTTP implementations.

`src.responses_adapter` converts history and function schemas, and normalizes
typed output into `LLMResponse`. `src.llm_core` translates text, completed tool
calls, usage, finish status, and errors into Odysseus's existing SSE events.
Calls use `call_id`, not the distinct output item's `id`. Arguments are accumulated
by output index and validated as JSON objects; incomplete, duplicate, malformed,
or unavailable calls produce an error before any calls from that round execute.
Tools execute only after `response.completed`, through the existing agent
conversion, validation, authorization, confirmation, and execution pipeline.
Tool execution is outside provider retries. Interrupted streams have no success
completion and no executable partial calls; cancellation closes the HTTP stream.
Sampling controls are omitted on Responses so model defaults apply.

Conversation management remains local. Requests use `store: false`, request
`reasoning.encrypted_content`, and replay required output items followed by
`function_call_output` items. They never combine history with
`previous_response_id`; expired server-side state is irrelevant. The agent keeps
opaque replay data only during the current tool loop, consumes its internal
`responses_context` event, and does not emit it to the browser, log it, or store
it in assistant prose. Plaintext reasoning/summary events are ignored.

Normal completed turns, resumed approvals, and restarted sessions use Odysseus's
existing persisted text/tool-event history, starting a fresh stateless request.
Opaque reasoning is not persisted across those boundaries. Existing compaction
can remove old turns; reasoning replay follows the assistant/tool groups that
remain. On an explicitly configured provider/model switch, the adapter drops foreign
encrypted state and replays ordinary conversation, function calls, and results.

Usage maps `input_tokens` and `output_tokens` into existing metrics; output totals
include reasoning tokens. The transport also retains supplied cached-input and
reasoning-token detail fields. Existing agent aggregate metrics retain their
current total-token schema. Non-streaming utility calls still return text (and
optional actual-model metadata); native tool execution remains on the streaming
agent path. Built-in Responses tools and audio/file output are outside this
adapter's function-tool scope and unsupported output items fail explicitly.

## Verification and limits

`tests/test_llm_responses.py` mocks HTTP and SSE data and exercises multi-round
agent execution, correlation, stateless continuity, privacy, malformed arguments,
errors, retries, cancellation, custom URLs, and legacy configuration loading.
Existing transport and tool tests cover local/legacy behavior. These are
deterministic contract tests, not live compatibility certification of OpenAI,
LM Studio, Ollama, vLLM, or a third-party Responses implementation.

API references:

- [Migration and stateless reasoning](https://developers.openai.com/api/docs/guides/migrate-to-responses)
- [Function calling](https://developers.openai.com/api/docs/guides/function-calling)
- [Streaming](https://developers.openai.com/api/docs/guides/streaming-responses)

Validation on 2026-10-13 used the installed Odysseus runtime image (Python 3.14,
Node 20.19.2), an isolated source copy with repository-owned catalog fixtures,
an in-memory test database, and no production data, credentials, or network.

- Focused Responses, legacy transport, agent, tool, local-discovery, context,
  and session-persistence selection: **355 passed**.
- Full `python -m pytest -q`: **5,993 passed, 17 skipped, 10 failed**.
- CI syntax command `python -m compileall -q app.py core routes src services
  scripts tests` and `git diff --check`: passed. The runtime interpreter also
  parsed all 1,151 Python files successfully.

All ten full-suite failures were reproduced in an isolated baseline with the
pre-change LLM/settings/agent implementation and existing user edits retained:

| Existing failure area | Count | Observed cause |
| --- | ---: | --- |
| `test_calendar_reminder_minutes_parsing.py` | 6 | `_create_calendar_reminder` is undefined in existing calendar code |
| `test_gpu_compose_standalone.py` | 2 | Existing base Compose edits differ from standalone GPU configurations |
| `test_integrations_url_join.py` | 1 | Existing root URL join adds an unexpected trailing slash |
| `test_pr6020_browser_review_regressions.py` | 1 | Node 20 browser-import test lacks `navigator` |

The baseline failure selection had **71 passing tests and the same 10 failures**.
These unrelated components were left unchanged. The implementation test run
used no live provider requests or service deployment; activation requires
running the updated code and adding the endpoint/model override.

Follow-up activation on 2026-10-13 verified the rebuilt `odysseus-custom:local`
container. Its empty `llm_protocol_overrides` still selected Chat Completions.
The existing OpenAI endpoint was configured for `gpt-6-luna`, `responses`, and
`medium` effort, preserving other settings and a private settings backup. A live
Responses request accepted that reasoning configuration with a dummy function
and returned one completed function call. No function was executed. Reported
usage was 60 input and 14 output tokens; this simple probe used zero reasoning
tokens, so it verifies acceptance of the reasoning/tool configuration rather
than the quality of a reasoning-heavy tool workflow.
