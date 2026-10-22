"""Deterministic wire/agent-boundary coverage for opt-in Responses transport."""

import asyncio
import copy
import json

import httpx
import pytest
from fastapi import HTTPException

from src import llm_core, settings
from src.responses_adapter import build_input, build_payload, normalize_response, protocol_options

URL = "https://api.openai.com/v1/chat/completions"
MODEL = "gpt-6-luna"
MESSAGES = [{"role": "system", "content": "Help."}, {"role": "user", "content": "Search."}]
TOOLS = [{"type": "function", "function": {"name": "web_search", "description": "Search",
          "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}}]


def text_item(text="Hello"):
    return {"id": "msg_1", "type": "message", "role": "assistant", "status": "completed",
            "content": [{"type": "output_text", "text": text, "annotations": []}]}


def call_item(call_id="call_1", arguments='{"query":"hi"}'):
    return {"id": "fc_" + call_id, "type": "function_call", "call_id": call_id,
            "name": "web_search", "arguments": arguments, "status": "completed"}


def response_data(output=None, **overrides):
    return {"id": "resp_1", "status": "completed", "model": MODEL,
            "output": [text_item()] if output is None else output,
            "usage": {"input_tokens": 12, "output_tokens": 9,
                      "output_tokens_details": {"reasoning_tokens": 5}}, **overrides}


def completion(output=None, **overrides):
    return {"type": "response.completed", "response": response_data(output, **overrides)}


class FakeResponse:
    def __init__(self, events, status=200, error=None, on_close=None):
        self.events, self.status_code, self.error, self.on_close = events, status, error, on_close

    async def aiter_lines(self):
        for event in self.events:
            if isinstance(event, BaseException):
                raise event
            yield "event: " + event["type"]
            yield "data: " + json.dumps(event)
            yield ""

    async def aread(self):
        return json.dumps({"error": {"message": self.error or "Rejected"}}).encode()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        if self.on_close:
            self.on_close()


class FakeClient:
    def __init__(self, rounds, status=200):
        self.rounds, self.status = iter(rounds), status
        self.requests, self.closed = [], 0

    def stream(self, method, url, **kwargs):
        self.requests.append((url, kwargs))
        return FakeResponse(next(self.rounds), self.status,
                            on_close=lambda: setattr(self, "closed", self.closed + 1))


@pytest.fixture(autouse=True)
def isolated_transport(monkeypatch):
    monkeypatch.setattr(settings, "get_setting", lambda key, default=None: default)
    monkeypatch.setattr(llm_core, "_is_host_dead", lambda url: False)
    monkeypatch.setattr(llm_core, "note_model_activity", lambda *args: None)
    monkeypatch.setattr(llm_core, "_clear_host_dead", lambda *args: None)
    monkeypatch.setattr(llm_core, "_mark_host_dead", lambda *args: False)


def events(chunks):
    return [json.loads(line[6:]) for chunk in chunks for line in chunk.splitlines()
            if line.startswith("data: ") and line != "data: [DONE]"]


async def stream(client, monkeypatch, messages=MESSAGES, **kwargs):
    monkeypatch.setattr(llm_core, "_get_http_client", lambda: client)
    return [chunk async for chunk in llm_core.stream_llm(
        URL, MODEL, messages, tools=TOOLS, api_mode="responses", reasoning_effort="medium", **kwargs)]


@pytest.mark.parametrize("url", [URL, "http://localhost:11434/v1/chat/completions",
                                "http://localhost:1234/v1/chat/completions", "https://proxy.example/prefix/v1/chat/completions"])
def test_legacy_protocol_default_is_independent_of_model(url):
    assert protocol_options(url, MODEL) == ("chat_completions", None)


def test_exact_endpoint_and_model_configuration(monkeypatch):
    monkeypatch.setattr(settings, "get_setting", lambda *args: [
        {"base_url": "https://api.openai.com/v1", "model": MODEL, "api_mode": "responses", "reasoning_effort": "medium"}])
    assert protocol_options(URL, MODEL) == ("responses", "medium")
    assert protocol_options(URL, "gpt-4o") == ("chat_completions", None)
    assert protocol_options("https://proxy.example/v1", MODEL) == ("chat_completions", None)
    assert protocol_options(URL, MODEL, "chat_completions", "none") == ("chat_completions", "none")


@pytest.mark.parametrize("mode,effort", [("auto", None), ([], None), ("responses", "ultra"), ("responses", [])])
def test_invalid_configuration_is_actionable(mode, effort):
    with pytest.raises((ValueError, TypeError)):
        protocol_options(URL, MODEL, mode, effort)


def test_reasoning_tools_payload_preserves_existing_schemas():
    original = copy.deepcopy(TOOLS)
    payload = build_payload(MODEL, MESSAGES, TOOLS, effort="medium", max_tokens=100)
    assert payload["reasoning"] == {"effort": "medium"}
    assert payload["max_output_tokens"] == 100
    assert payload["store"] is False
    assert payload["include"] == ["reasoning.encrypted_content"]
    assert payload["tools"][0]["name"] == "web_search"
    assert payload["tools"][0]["strict"] is False
    assert "function" not in payload["tools"][0]
    assert "temperature" not in payload
    assert payload["input"] == MESSAGES
    assert TOOLS == original


def test_developer_assistant_and_image_input():
    messages = [{"role": "developer", "content": "Answer briefly"}, {"role": "assistant", "content": "OK"},
                {"role": "user", "content": [{"type": "text", "text": "Read"},
                 {"type": "image_url", "image_url": {"url": "data:image/png;base64,eA==", "detail": "low"}}]}]
    converted = build_input(messages)
    assert converted[:2] == messages[:2]
    assert converted[2]["content"][1] == {"type": "input_image", "image_url": "data:image/png;base64,eA==", "detail": "low"}


def test_text_and_tool_only_normalization():
    assert normalize_response(response_data()).text == "Hello"
    result = normalize_response(response_data([call_item()]))
    assert result.text == ""
    assert result.tool_calls == [{"id": "call_1", "name": "web_search", "arguments": '{"query":"hi"}'}]
    assert result.response_id == "resp_1"


@pytest.mark.parametrize("args", ["{", "[]", "null", "true", 12])
def test_malformed_function_arguments_never_become_calls(args):
    with pytest.raises(ValueError, match="Invalid JSON object arguments"):
        normalize_response(response_data([call_item(arguments=args)]))


def test_duplicate_call_id_is_rejected():
    with pytest.raises(ValueError, match="duplicate"):
        normalize_response(response_data([call_item(), call_item()]))


async def test_streaming_text_usage_and_reasoning_privacy(monkeypatch):
    reasoning = {"type": "reasoning", "id": "rs_1", "encrypted_content": "opaque-secret",
                 "summary": [{"type": "summary_text", "text": "private reasoning"}]}
    client = FakeClient([[{"type": "response.output_text.delta", "delta": "Hel"},
                          {"type": "response.reasoning_summary_text.delta", "delta": "private reasoning"},
                          {"type": "response.output_text.delta", "delta": "lo"}, completion([reasoning, text_item()])]])
    chunks = await stream(client, monkeypatch)
    assert [event["delta"] for event in events(chunks) if "delta" in event] == ["Hel", "lo"]
    assert "private reasoning" not in "".join(chunks)
    assert "opaque-secret" not in "".join(chunks)
    usage = next(event["data"] for event in events(chunks) if event.get("type") == "usage")
    assert usage["input_tokens"] == 12 and usage["output_tokens"] == 9
    assert usage["output_tokens_details"]["reasoning_tokens"] == 5
    assert chunks[-1] == "data: [DONE]\n\n"
    assert client.closed == 1


async def test_interleaved_streaming_calls_release_only_after_completion(monkeypatch):
    calls = [call_item("call_A"), call_item("call_B")]
    wire = []
    for index, call in enumerate(calls):
        wire.append({"type": "response.output_item.added", "output_index": index, "item": {**call, "arguments": "", "status": "in_progress"}})
    for index in [1, 0]:
        wire.append({"type": "response.function_call_arguments.delta", "output_index": index, "delta": '{"query":'})
        wire.append({"type": "response.function_call_arguments.delta", "output_index": index, "delta": '"hi"}'})
        wire.append({"type": "response.output_item.done", "output_index": index, "item": calls[index]})
    wire.append({"type": "response.completed", "response": {"id": "resp_1", "status": "completed"}})
    chunks = await stream(FakeClient([wire]), monkeypatch)
    tool_events = [event for event in events(chunks) if event.get("type") == "tool_calls"]
    assert len(tool_events) == 1
    assert [call["id"] for call in tool_events[0]["calls"]] == ["call_A", "call_B"]


async def test_multiple_and_consecutive_tool_rounds_replay_call_ids(monkeypatch):
    from src.agent_loop import _append_tool_results, _resolve_tool_blocks

    reasoning = {"type": "reasoning", "id": "rs_1", "encrypted_content": "opaque-secret", "summary": []}
    client = FakeClient([[completion([reasoning, call_item("A"), call_item("B")])],
                         [completion([call_item("C")])], [completion()]])
    messages = copy.deepcopy(MESSAGES)
    for round_num in [1, 2]:
        chunks = await stream(client, monkeypatch, messages)
        parsed = events(chunks)
        native = next(event["calls"] for event in parsed if event.get("type") == "tool_calls")
        replay = next(event["items"] for event in parsed if event.get("type") == "responses_context")
        blocks, used_native, converted = _resolve_tool_blocks("", native, round_num)
        assert used_native and len(blocks) == len(native)
        results = ["result-" + call["id"] for call in converted]
        _append_tool_results(messages, "", converted, results, results, used_native, round_num, response_items=replay)
    chunks = await stream(client, monkeypatch, messages)
    assert any(event.get("delta") == "Hello" for event in events(chunks))
    final = client.requests[-1][1]["json"]
    assert "previous_response_id" not in final
    assert len([item for item in final["input"] if item.get("type") == "reasoning"]) == 1
    outputs = [item for item in final["input"] if item.get("type") == "function_call_output"]
    assert [(item["call_id"], item["output"]) for item in outputs] == [("A", "result-A"), ("B", "result-B"), ("C", "result-C")]
    legacy = llm_core._sanitize_llm_messages(messages)
    assert all("_responses_output" not in message for message in legacy)


@pytest.mark.parametrize("wire", [
    [{"type": "response.output_item.added", "output_index": 0, "item": call_item()}],
    [completion([call_item(arguments="{")])],
    [completion([call_item()], status="incomplete")],
    [{"type": "response.failed", "response": {"error": {"message": "Bad schema", "code": "invalid_request_error"}}}],
    [{"type": "error", "message": "Rejected", "code": "invalid_request_error"}],
    [httpx.ReadError("Disconnected")],
])
async def test_failed_or_interrupted_stream_has_no_executable_calls(monkeypatch, wire):
    client = FakeClient([wire])
    chunks = await stream(client, monkeypatch)
    assert any(chunk.startswith("event: error") for chunk in chunks)
    assert not any(event.get("type") == "tool_calls" for event in events(chunks))
    assert "data: [DONE]\n\n" not in chunks
    assert client.closed == 1


@pytest.mark.parametrize("status", [400, 401, 403, 404, 429, 503])
async def test_http_errors_preserve_status(monkeypatch, status):
    client = FakeClient([[]], status=status)
    chunks = await stream(client, monkeypatch)
    assert events(chunks)[0]["status"] == status
    assert not any(event.get("type") == "tool_calls" for event in events(chunks))


async def test_stream_cancellation_closes_transport(monkeypatch):
    client = FakeClient([[asyncio.CancelledError()]])
    with pytest.raises(asyncio.CancelledError):
        await stream(client, monkeypatch)
    assert client.closed == 1


def test_sync_responses_custom_base_headers_and_timeout(monkeypatch):
    seen = {}
    def post(url, headers=None, **kwargs):
        seen.update(url=url, headers=headers, **kwargs)
        return httpx.Response(200, json=response_data(), request=httpx.Request("POST", url))
    monkeypatch.setattr(llm_core.httpx, "post", post)
    result = llm_core.llm_call("https://proxy.example/custom/v1", MODEL, MESSAGES, api_mode="responses",
                               reasoning_effort="medium", headers={"Authorization": "Bearer test", "X-Custom": "value"}, timeout=43)
    assert result == "Hello"
    assert seen["url"] == "https://proxy.example/custom/v1/responses"
    assert seen["headers"]["X-Custom"] == "value"
    assert seen["headers"]["Authorization"] == "Bearer test"
    assert seen["timeout"].read == 43


async def test_async_responses_transient_retry_and_actual_model(monkeypatch):
    requests = []
    async def post(client, url, headers, **kwargs):
        requests.append(kwargs["json"])
        return httpx.Response(429 if len(requests) == 1 else 200, json=response_data(model="gpt-6-luna-snapshot"), request=httpx.Request("POST", url))
    monkeypatch.setattr(llm_core, "httpx_post_kimi_aware_async", post)
    monkeypatch.setattr(llm_core.LLMConfig, "RETRY_DELAY", 0)
    result = await llm_core.llm_call_async(URL, MODEL, MESSAGES, api_mode="responses", max_retries=2, return_model_metadata=True)
    assert result == ("Hello", "gpt-6-luna-snapshot")
    assert len(requests) == 2


async def test_async_invalid_effort_is_not_retried_or_downgraded(monkeypatch):
    requests = []
    async def post(client, url, headers, **kwargs):
        requests.append(kwargs["json"])
        return httpx.Response(400, json={"error": {"message": "Unsupported reasoning effort"}}, request=httpx.Request("POST", url))
    monkeypatch.setattr(llm_core, "httpx_post_kimi_aware_async", post)
    with pytest.raises(HTTPException) as error:
        await llm_core.llm_call_async(URL, MODEL, MESSAGES, api_mode="responses", reasoning_effort="max")
    assert error.value.status_code == 400
    assert error.value.fallback_eligible is False
    assert len(requests) == 1 and requests[0]["reasoning"] == {"effort": "max"}


def test_legacy_settings_file_needs_no_migration(tmp_path, monkeypatch):
    file = tmp_path / "settings.json"
    file.write_text(json.dumps({"default_model": "local-model", "agent_email_confirm": True}))
    monkeypatch.setattr(settings, "SETTINGS_FILE", str(file))
    monkeypatch.setattr(settings, "_settings_cache", None)
    loaded = settings.load_settings()
    assert loaded["default_model"] == "local-model"
    assert loaded["llm_protocol_overrides"] == []
    assert loaded["agent_email_confirm"] is True


def test_persisted_history_can_start_stateless_responses_turn():
    from core.models import ChatMessage
    history = [ChatMessage("user", "Previous question"), ChatMessage("assistant", "Previous answer"), ChatMessage("user", "Continue")]
    payload = build_payload(MODEL, [message.to_dict() for message in history])
    assert [item["content"] for item in payload["input"]] == ["Previous question", "Previous answer", "Continue"]
    assert "previous_response_id" not in payload


async def test_real_agent_uses_existing_executor_and_hides_replay(monkeypatch):
    from src import agent_loop

    reasoning = {"type": "reasoning", "id": "rs_1", "encrypted_content": "opaque-secret", "summary": []}
    client = FakeClient([[completion([reasoning, call_item("agent-call")])], [completion([text_item("Search complete.")])]])
    monkeypatch.setattr(llm_core, "_get_http_client", lambda: client)
    def get_setting(key, default=None):
        if key == "llm_protocol_overrides":
            return [{"base_url": "https://api.openai.com/v1", "model": MODEL, "api_mode": "responses", "reasoning_effort": "medium"}]
        return default
    monkeypatch.setattr(settings, "get_setting", get_setting)
    monkeypatch.setattr(agent_loop, "get_setting", get_setting)
    monkeypatch.setattr(agent_loop, "get_mcp_manager", lambda: None)
    monkeypatch.setattr(agent_loop, "estimate_tokens", lambda *args, **kwargs: 10)
    monkeypatch.setattr(agent_loop, "blocked_tools_for_owner", lambda owner: set())
    dispatched = []
    async def execute(block, *args, **kwargs):
        dispatched.append(block)
        return "web_search", {"output": "Result: Found https://example.com", "exit_code": 0}
    monkeypatch.setattr(agent_loop, "execute_tool_block", execute)
    chunks = [chunk async for chunk in agent_loop.stream_agent_loop(
        URL, MODEL, [{"role": "user", "content": "Search the web for hi and report."}],
        max_rounds=3, relevant_tools={"web_search"}, fallbacks=[], _is_teacher_run=True)]
    assert len(dispatched) == 1
    assert dispatched[0].tool_type == "web_search"
    assert len(client.requests) == 2
    assert "responses_context" not in "".join(chunks)
    assert "opaque-secret" not in "".join(chunks)
    final_input = client.requests[1][1]["json"]["input"]
    assert any(item.get("call_id") == "agent-call" and item.get("type") == "function_call_output" for item in final_input)
    assert any("Search complete." in chunk for chunk in chunks)


async def test_closing_partially_consumed_stream_closes_transport(monkeypatch):
    client = FakeClient([[{"type": "response.output_text.delta", "delta": "partial"}, completion()]])
    monkeypatch.setattr(llm_core, "_get_http_client", lambda: client)
    generator = llm_core.stream_llm(URL, MODEL, MESSAGES, api_mode="responses")
    assert "partial" in await anext(generator)
    await generator.aclose()
    assert client.closed == 1


async def test_explicit_chat_reasoning_is_never_downgraded(monkeypatch):
    class Client(FakeClient):
        def stream(self, method, url, **kwargs):
            self.requests.append((url, kwargs))
            class Response(FakeResponse):
                async def aiter_lines(self):
                    yield 'data: {"choices":[{"delta":{"content":"OK"}}]}'
                    yield 'data: [DONE]'
            return Response([])
    client = Client([])
    monkeypatch.setattr(llm_core, "_get_http_client", lambda: client)
    chunks = [chunk async for chunk in llm_core.stream_llm(
        URL, "gpt-5.6-luna", MESSAGES, tools=TOOLS, api_mode="chat_completions", reasoning_effort="high")]
    assert any("OK" in chunk for chunk in chunks)
    assert client.requests[0][1]["json"]["reasoning_effort"] == "high"


def test_explicit_legacy_effort_partitions_response_cache(monkeypatch):
    monkeypatch.setattr(llm_core, "_response_cache", {})
    requests = []
    def post(url, headers=None, **kwargs):
        effort = kwargs["json"]["reasoning_effort"]
        requests.append(effort)
        return httpx.Response(200, json={"choices": [{"message": {"content": effort}}]}, request=httpx.Request("POST", url))
    monkeypatch.setattr(llm_core.httpx, "post", post)
    for effort in ["high", "medium", "high"]:
        assert llm_core.llm_call(URL, MODEL, MESSAGES, reasoning_effort=effort) == effort
    assert requests == ["high", "medium"]


def test_encrypted_replay_is_not_sent_to_another_route():
    reasoning = {"type": "reasoning", "id": "rs_1", "encrypted_content": "opaque-secret", "summary": []}
    messages = [{"role": "assistant", "content": None, "tool_calls": [
        {"id": "A", "type": "function", "function": {"name": "web_search", "arguments": "{}"}}],
        "_responses_output": [reasoning, call_item("A", "{}")],
        "_responses_origin": {"base_url": "https://api.openai.com/v1", "model": MODEL}},
        {"role": "tool", "tool_call_id": "A", "content": "Found"}]
    _, same = llm_core._responses_request(URL, MODEL, messages)
    _, other = llm_core._responses_request("https://proxy.example/v1", MODEL, messages)
    assert "opaque-secret" in json.dumps(same)
    assert "opaque-secret" not in json.dumps(other)
    assert other["input"][-1] == {"type": "function_call_output", "call_id": "A", "output": "Found"}


async def test_incomplete_text_has_length_finish_and_no_calls(monkeypatch):
    client = FakeClient([[{"type": "response.incomplete", "response": response_data([text_item("Partial")])}]])
    chunks = await stream(client, monkeypatch)
    assert {"type": "finish", "reason": "length"} in events(chunks)
    assert any(event.get("delta") == "Partial" for event in events(chunks))
    assert not any(event.get("type") == "tool_calls" for event in events(chunks))


def test_refusal_is_visible_text():
    item = text_item()
    item["content"] = [{"type": "refusal", "refusal": "Cannot help."}]
    assert normalize_response(response_data([item])).text == "Cannot help."


def test_stateless_reasoning_requires_encrypted_replay():
    with pytest.raises(ValueError, match="encrypted reasoning"):
        normalize_response(response_data([{"type": "reasoning", "id": "rs_1", "summary": []}, call_item()]))


async def test_invalid_configuration_emits_one_terminal_error(monkeypatch):
    client = FakeClient([])
    monkeypatch.setattr(llm_core, "_get_http_client", lambda: client)
    chunks = [chunk async for chunk in llm_core.stream_llm(URL, MODEL, MESSAGES, api_mode=[])]
    assert len(chunks) == 1
    assert events(chunks)[0]["status"] == 400
    assert events(chunks)[0]["fallback_eligible"] is False
    assert client.requests == []
