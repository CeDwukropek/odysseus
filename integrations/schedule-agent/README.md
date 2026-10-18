# Native personal schedule integration

Routes: `GET /api/schedule/capabilities`, `POST /api/schedule/chat`.
Requires native authentication (cookie or `ody_` token with chat scope), the
owner's agent/model privileges, and a separately verified `X-Schedule-Context`.
The configured gateway checks the signed account, conversation and operation.

Operator configuration:

```
SCHEDULE_GATEWAY_URL=https://your-schedule-backend
SCHEDULE_MODEL_ENDPOINT_IDS=registered-local-endpoint-id
SCHEDULE_DEFAULT_MODEL=your-local-tool-capable-model
ALLOWED_ORIGINS=https://your-schedule-site
```

Only enabled local endpoints on the explicit allowlist and visible to the
native owner are offered. No fallback to hosted models. The native transport
`stream_llm` executes a bounded loop with only the gateway's canonical tools.
The request-scoped tools are never registered in the unrestricted global agent
or MCP registry. `SKILL.md` is loaded from this reviewed checkout for each turn.

Capabilities mark current endpoints with `provider: local`, `execution: local`
and a human-readable `destination`. The browser can display cloud-provider
metadata when that support is activated in a later change; this preparation
does not permit cloud execution. Both OpenAI and Google destinations still
fail the runtime's local-only check, including if a catalogue is incorrect.

Opaque `extra_content` on model tool calls is replayed within the current
request, as required by Gemini thought signatures. It is not included in
the browser result or the text-only transcript.

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
  python -m pytest tests/test_schedule_bridge.py -q
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
