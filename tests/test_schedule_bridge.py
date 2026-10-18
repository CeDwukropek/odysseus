"""Integration boundaries exercised without contacting any model or user data."""
import asyncio
import json
import sys
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from src.schedule_bridge import (ScheduleGateway, local_model_url, native_completion,
    run_schedule_agent, selected_group, session_identity)


def run(coroutine):
    return asyncio.run(coroutine)


def test_identity_separates_both_accounts_and_conversation():
    baseline = session_identity("owner", "firebase-a", "conversation")
    assert baseline == session_identity("owner", "firebase-a", "conversation")
    assert len({baseline, session_identity("other", "firebase-a", "conversation"),
                session_identity("owner", "firebase-b", "conversation"),
                session_identity("owner", "firebase-a", "other")}) == 4


@pytest.mark.parametrize("field,value", [("provider", "n8n"), ("session_id", "other"),
    ("intent", "reschedule"), ("principal", ""), ("tools", None)])
def test_gateway_rejects_context_for_other_identity_or_operation(field, value):
    gateway = ScheduleGateway("http://fixed-gateway", "synthetic-context")
    async def request():
        return {"protocol": "wieik-schedule-v1", "provider": "odysseus", "session_id": "s",
                "intent": "makeup", "principal": "a", "tools": [], field: value}
    gateway.request = request
    with pytest.raises(HTTPException) as error:
        run(gateway.identity("s", "makeup"))
    assert error.value.status_code == 403


def test_only_configured_private_model_urls_are_eligible():
    for url in ["http://127.0.0.1:1234/v1", "http://100.83.39.75:1234/v1", "https://own.tail.ts.net"]:
        assert local_model_url(url)
    for url in ["https://api.openai.com/v1", "http://8.8.8.8/v1", "file:///tmp/a", "http://user:pass@localhost"]:
        assert not local_model_url(url)


def test_group_matching_includes_added_and_common_events_and_language_choice():
    groups = {"L": "L1", "Lek": "LekN"}
    assert selected_group({"group": "L2", "is_added": True}, groups)
    assert selected_group({"type": "wykład"}, groups)
    assert selected_group({"group": "L"}, groups)
    assert not selected_group({"group": "L2"}, groups)
    assert selected_group({"group": "Lek", "subject": "Język niemiecki"}, groups)
    assert not selected_group({"group": "Lek1", "subject": "Język angielski"}, groups)


def test_native_transport_parses_tools_and_never_exposes_context_token(monkeypatch):
    async def stream(*args, **kwargs):
        assert kwargs["headers"] == {"Authorization": "synthetic-provider"}
        assert "schedule-context" not in json.dumps(args)
        yield 'data: {"delta":"private reasoning","thinking":true}\n\n'
        yield 'data: {"delta":"Gotowe"}\n\n'
        yield 'data: {"type":"tool_calls","calls":[{"id":"1","name":"schedule_get_events","arguments":"{}"}]}\n\n'
        yield 'data: [DONE]\n\n'
    monkeypatch.setitem(sys.modules, "src.llm_core", SimpleNamespace(stream_llm=stream))
    result = run(native_completion({"url": "http://localhost", "model": "local", "headers": {
        "Authorization": "synthetic-provider"}}, [{"role": "user", "content": "hi"}], [], "s"))
    assert result["content"] == "Gotowe"
    assert result["calls"][0]["name"] == "schedule_get_events"


def test_makeup_uses_real_candidates_and_cannot_invoke_other_tools():
    executed, turns = [], iter([
        [{"name": "bash", "arguments": "{}"}, {"name": "schedule_create_override", "arguments": "{}"}],
        [{"name": "schedule_find_event", "arguments": json.dumps({"faculty": "TEST", "query": "Test"})}],
        []])
    async def completion(endpoint, messages, tools, sid):
        assert all(tool["function"]["name"] == "schedule_find_event" for tool in tools)
        return {"content": "Możliwe terminy", "calls": next(turns)}
    async def request(body):
        executed.append(body["tool"])
        return {"events": [{"id": "existing-id", "faculty": "TEST", "subject": "Test", "date": "2099-01-01",
                            "group": "L2", "type": "laboratorium", "status": "aktywne"},
                           {"id": "own", "date": "2099-01-01", "group": "L1", "status": "aktywne"}]}
    body = SimpleNamespace(faculty="TEST", groups={"L": "L1"}, intent="makeup", message="Odrobię Test")
    identity = {"tools": [{"name": "schedule.find_event", "description": "find", "inputSchema": {}}]}
    result = run(run_schedule_agent(body, identity, SimpleNamespace(request=request), {}, [], "s", completion=completion))
    assert executed == ["schedule.find_event"]
    assert result["proposals"] == []
    assert [row["id"] for row in result["candidates"]] == ["existing-id"]


def test_tool_metadata_survives_only_inside_the_request():
    signature = {"google": {"thought_signature": "synthetic-opaque-signature"}}
    turns = []
    async def completion(endpoint, messages, tools, sid):
        turns.append(len(messages))
        if len(turns) == 1:
            return {"content": "", "calls": [{"id": "call-1", "name": "schedule_find_event",
                "arguments": '{"faculty":"TEST","query":"Test"}', "extra_content": signature}]}
        assistant = next(message for message in messages if message.get("tool_calls"))
        assert assistant["tool_calls"][0]["extra_content"] == signature
        assert messages[-1]["role"] == "tool"
        assert messages[-1]["tool_call_id"] == "call-1"
        return {"content": "Gotowe", "calls": []}
    async def request(body):
        return {"events": []}
    body = SimpleNamespace(faculty="TEST", groups={}, intent="query", message="Test")
    identity = {"tools": [{"name": "schedule.find_event", "description": "find", "inputSchema": {}}]}
    result = run(run_schedule_agent(body, identity, SimpleNamespace(request=request), {}, [], "s", completion=completion))
    assert result["output"] == "Gotowe"
    assert "synthetic-opaque-signature" not in json.dumps(result)
    assert len(turns) == 2


def test_makeup_conflicts_include_selected_group_and_added_events():
    from routes.schedule_routes import makeup_cards
    async def request(body):
        return {"events": [{"id": "added", "start_time": "09:00:00", "duration_min": 90,
                            "is_added": True, "group": "L2"}], "next_offset": None}
    slots = run(makeup_cards([{"id": "real", "date": "2099-01-01", "start_time": "10:00:00", "duration_min": 90}],
                            SimpleNamespace(request=request), SimpleNamespace(faculty="TEST", groups={"L": "L1"})))
    assert slots[0]["event_id"] == "real"
    assert slots[0]["conflict_status"] == "conflict"


def native_client(monkeypatch, *, scopes=None, owner="owner"):
    import routes.schedule_routes as routes
    app = FastAPI()
    app.state.auth_manager = SimpleNamespace(get_privileges=lambda _: {"can_use_agent": True})
    @app.middleware("http")
    async def auth(request, call_next):
        request.state.current_user = "api"
        request.state.api_token = True
        request.state.api_token_owner = owner
        request.state.api_token_scopes = scopes if scopes is not None else ["chat"]
        return await call_next(request)
    app.include_router(routes.setup_schedule_routes(SimpleNamespace(), SimpleNamespace()))
    return TestClient(app)


def test_native_routes_require_chat_scope_and_human_owner(monkeypatch):
    assert native_client(monkeypatch, scopes=["models"]).get("/api/schedule/capabilities").status_code == 403
    assert native_client(monkeypatch, owner=None).get("/api/schedule/capabilities").status_code == 403


def test_capabilities_report_local_destination_without_credentials(monkeypatch):
    import routes.schedule_routes as routes
    row = SimpleNamespace(id="local", name="Own model server", api_key="synthetic-secret")
    monkeypatch.setattr(routes, "configured_endpoints", lambda owner: [row])
    monkeypatch.setattr(routes, "_endpoint_enabled_models", lambda endpoint: ["test-model"])
    response = native_client(monkeypatch).get("/api/schedule/capabilities")
    assert response.status_code == 200
    assert response.json()["endpoints"] == [{"id": "local", "name": "Own model server",
        "provider": "local", "execution": "local", "destination": "Twój serwer modeli", "models": ["test-model"]}]
    assert "synthetic-secret" not in response.text


@pytest.mark.parametrize("base", ["https://api.openai.com/v1", "https://generativelanguage.googleapis.com/v1beta/openai"])
def test_cloud_execution_remains_blocked_even_if_catalogue_is_incorrect(monkeypatch, base):
    import routes.schedule_routes as routes
    monkeypatch.setenv("SCHEDULE_GATEWAY_URL", "http://fixed-gateway")
    row = SimpleNamespace(id="cloud", base_url=base)
    monkeypatch.setattr(routes, "configured_endpoints", lambda owner: [row])
    monkeypatch.setattr(routes, "_endpoint_enabled_models", lambda endpoint: ["cloud-test-model"])
    monkeypatch.setattr(routes, "resolve_endpoint_runtime", lambda endpoint, owner: (base, "synthetic-secret"))
    async def identity(self, session, intent):
        return {"principal": "synthetic"}
    monkeypatch.setattr(routes.ScheduleGateway, "identity", identity)
    response = native_client(monkeypatch).post("/api/schedule/chat", headers={"X-Schedule-Context": "wks1.synthetic"},
        json={"message": "Test", "faculty": "TEST", "session_id": "s", "endpoint_id": "cloud", "model": "cloud-test-model"})
    assert response.status_code == 403
    assert "synthetic-secret" not in response.text


def test_native_route_rejects_origin_and_unconfigured_cloud_models(monkeypatch):
    import routes.schedule_routes as routes
    monkeypatch.setenv("ALLOWED_ORIGINS", "https://schedule.test")
    monkeypatch.setenv("SCHEDULE_GATEWAY_URL", "http://fixed-gateway")
    monkeypatch.setenv("SCHEDULE_MODEL_ENDPOINT_IDS", "")
    calls = []
    async def identity(self, session, intent):
        calls.append("identity")
        return {"principal": "synthetic"}
    monkeypatch.setattr(routes.ScheduleGateway, "identity", identity)
    client = native_client(monkeypatch)
    body = {"message": "hi", "faculty": "TEST", "session_id": "s", "model": "cloud-model"}
    headers = {"X-Schedule-Context": "wks1.synthetic"}
    assert client.post("/api/schedule/chat", json=body, headers={**headers, "Origin": "https://other.test"}).status_code == 403
    assert calls == []
    assert client.post("/api/schedule/chat", json=body, headers=headers).status_code == 422
    assert calls == ["identity"]


def test_native_route_keeps_only_text_history_and_binds_native_account(monkeypatch):
    import routes.schedule_routes as routes
    import routes.chat_helpers as chat_helpers
    monkeypatch.setenv("SCHEDULE_GATEWAY_URL", "http://fixed-gateway")
    row = SimpleNamespace(id="local", base_url="http://127.0.0.1:1234")
    monkeypatch.setattr(routes, "configured_endpoints", lambda owner: [row])
    monkeypatch.setattr(routes, "_endpoint_enabled_models", lambda row: ["local-model"])
    monkeypatch.setattr(routes, "resolve_endpoint_runtime", lambda row, owner: (row.base_url, "synthetic-provider"))
    monkeypatch.setattr(chat_helpers, "_enforce_chat_privileges", lambda *args: None)
    async def identity(self, session, intent):
        return {"principal": "firebase-a"}
    async def agent(*args):
        return {"output": "Test response", "proposals": [], "candidates": []}
    monkeypatch.setattr(routes.ScheduleGateway, "identity", identity)
    monkeypatch.setattr(routes, "run_schedule_agent", agent)
    sessions, persisted = {}, []
    def get(sid):
        return sessions[sid]
    def create(sid, name, url, model, owner):
        sessions[sid] = SimpleNamespace(owner=owner, history=[], model=model)
        return sessions[sid]
    manager = SimpleNamespace(get_session=get, create_session=create, add_message=lambda sid, message: persisted.append(message))
    app = FastAPI()
    @app.middleware("http")
    async def auth(request, call_next):
        request.state.current_user = "owner"
        return await call_next(request)
    app.include_router(routes.setup_schedule_routes(manager, SimpleNamespace()))
    response = TestClient(app).post("/api/schedule/chat", headers={"X-Schedule-Context": "wks1.synthetic"}, json={
        "message": "Test request", "faculty": "TEST", "session_id": "s", "model": "local-model"})
    assert response.status_code == 200
    assert response.json()["native_session_id"] == session_identity("owner", "firebase-a", "s")
    assert [message.content for message in persisted] == ["Test request", "Test response"]
    assert "synthetic-provider" not in repr(persisted)
    assert "wks1" not in repr(persisted)


def semantic_case(completion, request, intent="query"):
    tool = "schedule.find_makeup_options" if intent == "makeup" else "schedule.get_next_occurrence"
    body = SimpleNamespace(faculty="EiAs3", groups={"Lk": "Lk1"}, intent=intent,
                           message="kiedy mam najbliższe laby z Modelowania układów dynamicznych?")
    identity = {"selection": {"faculty": "EiAs3", "groups": {"Lk": "Lk1"}},
                "tools": [{"name": tool, "description": "semantic query", "inputSchema": {}}]}
    return run(run_schedule_agent(body, identity, SimpleNamespace(request=request), {}, [], "semantic", completion=completion))


def test_semantic_lookup_uses_backend_answer_even_when_model_claims_no_classes():
    calls = iter([[{"name": "schedule_get_next_occurrence", "arguments": '{"query":"Modelowania układów dynamicznych","kind":"laboratory"}'}], []])
    async def completion(endpoint, messages, tools, sid):
        assert "wks1" not in json.dumps(messages)
        return {"content": "Nie znalazłem żadnych zajęć", "calls": next(calls)}
    async def request(body):
        assert "groups" not in body["arguments"]
        return {"status": "found", "subject": "Modelowanie układów dynamicznych",
                "event": {"id": "actual", "date": "2026-10-19", "group": "Lk1"},
                "message": "Najbliższe zajęcia: 2026-10-19, 07:30, Lk1"}
    result = semantic_case(completion, request)
    assert result["output"] == "Najbliższe zajęcia: 2026-10-19, 07:30, Lk1"
    assert result["evidence"][0]["event"]["id"] == "actual"
    assert result["proposals"] == []


def test_semantic_query_retries_an_answer_without_reading_the_schedule():
    turns = iter([[], [{"name": "schedule_get_next_occurrence", "arguments": '{"query":"MUD"}'}], []])
    async def completion(*args):
        return {"content": "No classes", "calls": next(turns)}
    async def request(body):
        return {"status": "missing_groups", "message": "Wybierz grupę Lk w aplikacji."}
    assert semantic_case(completion, request)["output"] == "Wybierz grupę Lk w aplikacji."


def test_failed_read_is_not_presented_as_an_empty_schedule():
    turns = iter([[{"name": "schedule_get_next_occurrence", "arguments": '{"query":"MUD"}'}], []])
    async def completion(*args):
        return {"content": "Nie masz zajęć", "calls": next(turns)}
    async def request(body):
        raise HTTPException(503, "Backend unavailable")
    result = semantic_case(completion, request)
    assert result["output"].startswith("Nie udało się odczytać")
    assert result["evidence"][0]["status"] == "read_failed"


def test_makeup_keeps_backend_conflict_cards_and_cannot_mutate():
    turns = iter([[{"name": "schedule_find_makeup_options", "arguments": '{"query":"MUD","kind":"laboratory"}'}], []])
    async def completion(*args):
        return {"content": "Moved!", "calls": next(turns)}
    async def request(body):
        return {"status": "found", "message": "Wybierz istniejący termin.",
                "slots": [{"event_id": "real-source", "conflict_status": "conflict"}]}
    result = semantic_case(completion, request, "makeup")
    assert result["slots"][0]["conflict_status"] == "conflict"
    assert result["output"] == "Wybierz istniejący termin."
    assert result["proposals"] == []


def test_native_route_rejects_groups_not_matching_signed_selection(monkeypatch):
    import routes.schedule_routes as routes
    monkeypatch.setenv("SCHEDULE_GATEWAY_URL", "http://fixed-gateway")
    async def identity(self, session, intent):
        return {"principal": "synthetic", "selection": {"faculty": "EiAs3", "groups": {"Lk": "Lk1"}}}
    monkeypatch.setattr(routes.ScheduleGateway, "identity", identity)
    response = native_client(monkeypatch).post("/api/schedule/chat", headers={"X-Schedule-Context": "wks1.synthetic"},
        json={"message": "Test", "faculty": "EiAs3", "session_id": "s", "groups": {"Lk": "Lk5"}})
    assert response.status_code == 403
