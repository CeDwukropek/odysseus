"""Native Odysseus schedule integration with request-scoped delegated tools."""
import asyncio
import os

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from core.database import ModelEndpoint, SessionLocal
from core.models import ChatMessage
from src.auth_helpers import effective_user, owner_filter, require_chat_api_token_scope
from src.endpoint_resolver import _endpoint_enabled_models, build_chat_url, build_headers, normalize_base, resolve_endpoint_runtime
from src.schedule_models import chat_models, model_destination
from src.schedule_bridge import (PROTOCOL, INTENTS, ScheduleGateway, _locks,
                                 run_schedule_agent, session_identity)


class ScheduleChat(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: str = Field(min_length=1, max_length=8000)
    faculty: str = Field(min_length=1, max_length=256)
    session_id: str = Field(min_length=1, max_length=128)
    intent: str = "query"
    groups: dict[str, str] = Field(default_factory=dict, max_length=20)
    endpoint_id: str = Field(default="", max_length=128)
    model: str = Field(default="", max_length=128)
    use_memory: bool = False
    history: list = Field(default_factory=list, max_length=16)


def request_owner(request):
    owner = effective_user(request)
    if not owner or owner in {"api", "internal-tool"}:
        raise HTTPException(401, "Zaloguj się do Odysseusa lub podaj jego token z uprawnieniem chat.")
    return owner


def configured_endpoints(owner):
    allowed = {value.strip() for value in os.getenv("SCHEDULE_MODEL_ENDPOINT_IDS", "").split(",") if value.strip()}
    if not allowed:
        return []
    with SessionLocal() as db:
        query = db.query(ModelEndpoint).filter(ModelEndpoint.id.in_(allowed), ModelEndpoint.is_enabled.is_(True))
        rows = owner_filter(query, ModelEndpoint, owner).all()
        return [row for row in rows if row.model_type != "image" and row.supports_tools is not False
                and model_destination(row.base_url)]


def request_models(request, endpoint):
    from routes.chat_helpers import _allowed_models_for_request
    allowed = _allowed_models_for_request(request)
    return [model for model in chat_models(_endpoint_enabled_models(endpoint))
            if allowed is None or model in allowed]


async def makeup_cards(candidates, gateway, body):
    from src.schedule_bridge import selected_group
    slots, days = [], {}
    for row in sorted(candidates, key=lambda row: (row["date"], row["start_time"]))[:8]:
        date = row["date"]
        if date not in days:
            try:
                events, offset = [], 0
                while True:
                    page = await gateway.request({"tool": "schedule.get_events", "arguments": {
                        "faculty": body.faculty, "date_from": date, "date_to": date, "offset": offset}})
                    events.extend(event for event in page["events"] if selected_group(event, body.groups))
                    offset = page.get("next_offset")
                    if offset is None:
                        break
                days[date] = events
            except HTTPException:
                days[date] = None
        def minutes(value):
            parts = value.split(":")
            return int(parts[0]) * 60 + int(parts[1])
        start = minutes(row["start_time"])
        status = "unknown" if days[date] is None else "free"
        if days[date] is not None and any(event["id"] != row["id"] and
                minutes(event["start_time"]) < start + row["duration_min"] and
                start < minutes(event["start_time"]) + event["duration_min"] for event in days[date]):
            status = "conflict"
        slots.append({**row, "event_id": row["id"], "conflict_status": status})
    return slots


def setup_schedule_routes(session_manager, chat_processor):
    router = APIRouter(prefix="/api/schedule", tags=["schedule"], dependencies=[Depends(require_chat_api_token_scope)])

    @router.get("/capabilities")
    async def capabilities(request: Request):
        owner = request_owner(request)
        auth = getattr(request.app.state, "auth_manager", None)
        if auth and not auth.get_privileges(owner).get("can_use_agent", False):
            raise HTTPException(403, "Konto Odysseusa nie ma uprawnień do agenta.")
        return {"protocol": PROTOCOL, "confirmation": "browser-only", "intents": sorted(INTENTS),
                "endpoints": [{"id": row.id, "name": row.name, **model_destination(row.base_url), "models": models}
                    for row in configured_endpoints(owner) if (models := request_models(request, row))]}

    @router.post("/chat")
    async def chat(request: Request, body: ScheduleChat):
        owner = request_owner(request)
        auth = getattr(request.app.state, 'auth_manager', None)
        if auth and not auth.get_privileges(owner).get('can_use_agent', False):
            raise HTTPException(403, 'Konto Odysseusa nie ma uprawnień do agenta.')
        origin = request.headers.get("origin")
        if origin and origin not in {value.strip() for value in os.getenv("ALLOWED_ORIGINS", "").split(",")}:
            raise HTTPException(403, "Ta strona nie jest dopuszczona do lokalnego Odysseusa.")
        if body.intent not in INTENTS or any(len(k) > 32 or len(v) > 64 for k, v in body.groups.items()):
            raise HTTPException(422, "Nieprawidłowy kontekst planu.")
        token = request.headers.get("x-schedule-context", "")
        if len(token) > 2048 or not token.startswith("wks1."):
            raise HTTPException(401, "Brak ważnego kontekstu planu.")
        gateway_url = os.getenv("SCHEDULE_GATEWAY_URL", "")
        if not gateway_url:
            raise HTTPException(503, "Brak konfiguracji gateway planu.")
        gateway = ScheduleGateway(gateway_url, token)
        identity = await gateway.identity(body.session_id, body.intent)
        selection = identity.get("selection")
        if selection is not None and (selection.get("faculty") != body.faculty or selection.get("groups") != body.groups):
            raise HTTPException(403, "Wybrany plan lub grupy zmieniły się. Wyślij wiadomość ponownie.")
        endpoints = configured_endpoints(owner)
        if not body.endpoint_id and (len(endpoints) > 1 or any(
                model_destination(row.base_url)["execution"] == "cloud" for row in endpoints)):
            raise HTTPException(422, "Wybierz połączenie z modelem w ustawieniach AI.")
        endpoint = next((row for row in endpoints if row.id == body.endpoint_id), None) if body.endpoint_id else next(iter(endpoints), None)
        if endpoint is None:
            raise HTTPException(422, "Wybierz skonfigurowane połączenie z modelem w ustawieniach AI.")
        models = request_models(request, endpoint)
        model = body.model or os.getenv("SCHEDULE_DEFAULT_MODEL", "")
        if model not in models:
            raise HTTPException(422, "Wybierz model z listy Odysseusa.")
        base, key = resolve_endpoint_runtime(endpoint, owner=owner)
        destination = model_destination(base)
        if not destination or normalize_base(base) != normalize_base(endpoint.base_url):
            raise HTTPException(403, "Adres dostawcy nie odpowiada wybranemu połączeniu.")
        runtime = {"url": build_chat_url(base), "headers": build_headers(key, base), "model": model}
        sid = session_identity(owner, identity["principal"], body.session_id)
        if sid not in _locks and len(_locks) >= 2048:
            raise HTTPException(429, "Zbyt wiele otwartych rozmów planu.")
        lock = _locks.setdefault(sid, asyncio.Lock())
        if lock.locked():
            raise HTTPException(409, "Poprzednia wiadomość tej rozmowy jest nadal przetwarzana.")
        async with lock:
            try:
                sess = session_manager.get_session(sid)
            except KeyError:
                sess = session_manager.create_session(sid, "[Plan] " + body.faculty, runtime["url"], model, owner=owner)
            if sess.owner != owner:
                raise HTTPException(404, "Nie znaleziono rozmowy.")
            from routes.chat_helpers import _enforce_chat_privileges
            # Same model privilege gate as native chat; no cloud route fallback.
            original_model = sess.model
            sess.model = model
            try:
                _enforce_chat_privileges(request, sess)
            finally:
                sess.model = original_model
            history = [{"role": message.role, "content": message.content} for message in sess.history
                       if message.role in {"user", "assistant"} and isinstance(message.content, str)]
            preface = []
            if body.use_memory:
                # Explicit owner-scoped retrieval, no global skills or web tools.
                preface, *_ = chat_processor.build_context_preface(
                    body.message, sess, owner=owner, use_memory=True, use_rag=True,
                    use_web=False, use_skills=False, agent_mode=False)
            try:
                async with asyncio.timeout(180):
                    result = await run_schedule_agent(body, identity, gateway, runtime, history, sid, preface)
                    if body.intent == "makeup" and result.get("slots"):
                        result.update(intent="SLOTS", ui={"type": "slot_choices", "actions": True}, data={"slots": result["slots"]})
                    elif body.intent == "makeup" and result.get("candidates"):
                        slots = await makeup_cards(result["candidates"], gateway, body)
                        result.update(intent="SLOTS", ui={"type": "slot_choices", "actions": True}, data={"slots": slots})
            except TimeoutError:
                raise HTTPException(504, "Model przekroczył czas odpowiedzi. Wyślij krótsze pytanie.")
            except HTTPException:
                raise
            except Exception:
                raise HTTPException(503, "Agent nie odpowiedział. Sprawdź konfigurację wybranego modelu w Odysseusie.")
            result.pop("candidates", None)
            result.pop("slots", None)
            # Only transcript text is retained. No delegated tokens or provider headers.
            session_manager.add_message(sid, ChatMessage(role="user", content=body.message))
            session_manager.add_message(sid, ChatMessage(role="assistant", content=result["output"],
                metadata={"model": model, "schedule_integration": True}))
            return {"ok": True, "native_session_id": sid,
                    "model_route": {"endpoint_id": endpoint.id, "model": model, **destination}, **result}

    return router
