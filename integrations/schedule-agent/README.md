# Native personal schedule integration

Routes: `GET /api/schedule/capabilities`, `POST /api/schedule/chat`.
Requires native authentication (cookie or `ody_` token with chat scope), the
owner's agent/model privileges, and a separately verified `X-Schedule-Context`.
The configured gateway checks the signed account, conversation and operation.

Operator configuration:

```
SCHEDULE_GATEWAY_URL=https://your-schedule-backend
SCHEDULE_MODEL_ENDPOINT_IDS=registered-local-id,registered-openai-id,registered-google-id
SCHEDULE_DEFAULT_MODEL=your-local-tool-capable-model
ALLOWED_ORIGINS=https://your-schedule-site
```

Only enabled endpoints on the explicit allowlist and visible to the native
owner are offered: private model servers, official OpenAI API and Google Gemini
OpenAI-compatible API. Empty allowlist means no models. There is no fallback
to another endpoint or provider. The native transport
`stream_llm` executes a bounded loop with only the gateway's canonical tools.
The request-scoped tools are never registered in the unrestricted global agent
or MCP registry. `SKILL.md` is loaded from this reviewed checkout for each turn.

Capabilities identify `provider` (`local`, `openai`, `google`), `execution`
(`local` or `cloud`) and a human-readable `destination`, without credentials.
Models come from the existing cached/pinned/hidden configuration and respect
account model privileges. Known image/audio/embedding models are omitted;
a catalogue entry does not guarantee tool compatibility or API quota.

Cloud execution requires an explicit endpoint ID, even with only one API
configured. Multiple endpoints always require an explicit ID. Runtime resolves
credentials from Odysseus and verifies that the base URL still matches the
configured endpoint. Only HTTPS official API hosts/paths are accepted for cloud;
user credentials in URLs, queries, fragments and nonstandard cloud ports are
rejected. No provider keys are entered again in the schedule application.

Cloud providers receive the prompt, transcript, selected plan/groups, tool
results and, only with memory enabled, retrieved owner memory/documents. The
operator/user must authorize these destinations and data. This is inference
permission, not an opt-in to model training. Provider account terms and data
sharing settings govern training and retention independently. This integration
does not enable data sharing/training or change provider account settings.

Opaque `extra_content` on model tool calls is replayed within the current
request, as required by Gemini thought signatures. It is not included in
the browser result or the text-only transcript.

## Filtered plans and structured display

The gateway's `schedule.get_my_schedule` supports `kind` and `exclude_kinds`.
The bridge enforces unambiguous Polish requests such as "same ćwiczenia" and
"bez wykładów/labów" even if the model omits or contradicts the relevant argument.
It does not infer identity, groups or permissions. Complex requests still need
model interpretation; plural positive kinds are not reduced to one arbitrarily.

Verified date ranges and filters are stored in assistant-message metadata,
scoped to the gateway selection, without full tool results or credentials.
Filter-only follow-ups inherit dates from that verified context, while explicit
new periods are resolved separately. Client-provided history cannot set this
context. Structured rows are returned as `ui.type=schedule_plan`, `data.plan`;
the browser renders separate daily cards rather than a dense text paragraph.

Study-note retrieval and writing remain a separate planned feature. This branch
does not mount Obsidian or add unrestricted filesystem tools to the schedule agent.

The schedule gateway issues a five-minute context for query, makeup, reschedule,
cancel or restore. Query/makeup expose reads only. Other intents allow a matching
preview; confirmation remains an authenticated browser action. Makeup candidates
must be real future source occurrences; deterministic cards include conflicts
against selected groups and privately added events. Each session UUID binds the
native owner, schedule account and conversation. Client-supplied history is not
trusted; the runtime loads native owner-bound transcript history instead.

Memory/document retrieval is opt-in and owner-scoped. This does not automatically
index the schedule, ingest documents or extract memories from every conversation.
No tokens or provider headers are written into the transcript.

An operator can optionally set `ODYSSEUS_SESSION_COOKIE` for a separate test copy;
the default existing cookie name remains unchanged. Test copies must use separate
data/log mounts and should disable task scheduling and pollers.

Tests (use a disposable data directory and SQLite database):

```sh
DATABASE_URL=sqlite:////tmp/schedule-test.db ODYSSEUS_DATA_DIR=/tmp/ody-schedule-tests \
  python -m pytest tests/test_schedule_bridge.py tests/test_schedule_models.py -q
```

Verified against a staging schedule gateway and the user's local
`google/gemma-4-26b-a4b-qat`: read, two existing makeup candidates, and one
unconfirmed private room preview. This branch contains no live keys or data.

## Semantic schedule tools

Signed gateway selection is authoritative for faculty, groups and additional groups.
The bridge supports find_subjects, get_next_occurrence, get_my_schedule and
find_makeup_options. Query/makeup replies preserve the backend message and expose
evidence; backend-checked makeup conflicts are retained. Missing reads trigger one
retry and a clear unverified response instead of a claim that no events exist.

Bridge tests use controlled model responses. The new real Gemma conversation test
is deferred at the user's request because the local model endpoint is offline.

## Provider acceptance (2026-10-18)

Real staging tests passed with OpenAI `gpt-4.1-mini` and Google
`gemini-3.1-flash-lite`: a synthetic schedule identity asked about its next lab,
the model invoked a restricted semantic tool, and the reply matched the gateway's
verified source event. Memory was disabled and successful test transcripts were
removed. 49 Python adapter tests, 62 React tests and 24 Node contract tests passed.

Google `gemini-2.5-flash` returned 404 although listed in its catalogue. Provider
404, 429 and 401/403 errors now produce useful, sanitized messages without
exposing the raw response or falling back to another provider.
