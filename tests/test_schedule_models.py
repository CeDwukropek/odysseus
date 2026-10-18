import json
from types import SimpleNamespace

import pytest

from src.schedule_models import chat_models, model_destination


@pytest.mark.parametrize("url,provider", [
    ("https://api.openai.com/v1", "openai"), ("https://api.openai.com/v1/", "openai"),
    ("https://generativelanguage.googleapis.com/v1beta/openai", "google"),
    ("http://100.83.39.75:1234/v1", "local"), ("https://own.tail.ts.net/v1", "local"),
])
def test_approved_destinations(url, provider):
    assert model_destination(url)["provider"] == provider


@pytest.mark.parametrize("url", [
    "https://api.openai.com.evil.test/v1", "http://api.openai.com/v1",
    "https://user:secret@api.openai.com/v1", "https://api.openai.com:8443/v1",
    "https://api.openai.com/v1?key=secret", "https://api.openai.com/v1#fragment",
    "https://api.openai.com/unreviewed-path", "https://api.openai.com:invalid/v1",
    "https://generativelanguage.googleapis.com/v1beta", "https://other-cloud.test/v1", "https://[",
])
def test_unapproved_destinations(url):
    assert model_destination(url) is None


def test_catalogue_filters_non_chat_models():
    assert chat_models(["gpt-4.1-mini", "gemini-2.5-flash", "text-embedding-3-small",
        "gemini-2.5-flash-image", "gpt-audio", "gemini-2.5-pro-preview-tts", "whisper-1"]
    ) == ["gpt-4.1-mini", "gemini-2.5-flash"]


def test_endpoint_allowlist_and_owner_scope(monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    import routes.schedule_routes as routes
    from core.database import ModelEndpoint
    engine = create_engine("sqlite:///:memory:")
    ModelEndpoint.__table__.create(engine)
    factory = sessionmaker(bind=engine)
    rows = [
        ("own", "a", True, True, "llm"), ("other", "b", True, True, "llm"),
        ("shared", None, True, True, "llm"), ("disabled", "a", False, True, "llm"),
        ("no-tools", "a", True, False, "llm"), ("image", "a", True, True, "image"),
        ("not-allowed", "a", True, True, "llm"),
    ]
    with factory() as db:
        for id, owner, enabled, tools, kind in rows:
            db.add(ModelEndpoint(id=id, name=id, owner=owner, is_enabled=enabled,
                supports_tools=tools, model_type=kind, base_url="https://api.openai.com/v1",
                cached_models=json.dumps(["gpt-4.1-mini"])))
        db.commit()
    monkeypatch.setattr(routes, "SessionLocal", factory)
    monkeypatch.setenv("SCHEDULE_MODEL_ENDPOINT_IDS", "own,other,shared,disabled,no-tools,image")
    assert {row.id for row in routes.configured_endpoints("a")} == {"own", "shared"}
    monkeypatch.setenv("SCHEDULE_MODEL_ENDPOINT_IDS", "")
    assert routes.configured_endpoints("a") == []


def test_model_privileges_apply_to_catalogue(monkeypatch):
    import routes.schedule_routes as routes
    monkeypatch.setattr(routes, "_endpoint_enabled_models", lambda row: ["allowed", "forbidden", "image-test"])
    auth = SimpleNamespace(get_privileges=lambda owner: {"allowed_models": ["allowed"]})
    request = SimpleNamespace(state=SimpleNamespace(current_user="a"), app=SimpleNamespace(state=SimpleNamespace(auth_manager=auth)))
    assert routes.request_models(request, SimpleNamespace()) == ["allowed"]
    auth.get_privileges = lambda owner: {"block_all_models": True}
    assert routes.request_models(request, SimpleNamespace()) == []


@pytest.mark.parametrize("bases", [["https://api.openai.com/v1"],
    ["http://localhost:1234/v1", "https://generativelanguage.googleapis.com/v1beta/openai"]])
def test_cloud_and_multiple_endpoints_require_explicit_selection(monkeypatch, bases):
    from tests.test_schedule_bridge import native_client
    import routes.schedule_routes as routes
    monkeypatch.setenv("SCHEDULE_GATEWAY_URL", "http://fixed-gateway")
    monkeypatch.setattr(routes, "configured_endpoints", lambda owner: [SimpleNamespace(id=str(i), base_url=base)
        for i, base in enumerate(bases)])
    async def identity(self, session, intent):
        return {"principal": "synthetic"}
    monkeypatch.setattr(routes.ScheduleGateway, "identity", identity)
    response = native_client(monkeypatch).post("/api/schedule/chat", headers={"X-Schedule-Context": "wks1.synthetic"},
        json={"message": "Test", "faculty": "TEST", "session_id": "s", "model": "test-model"})
    assert response.status_code == 422
    assert "Wybierz połączenie" in response.json()["detail"]
