"""Managed mirror of the user's wieikschedule plan in the Odysseus calendar.

wieikschedule (Supabase) stays the only source of the plan. Odysseus pulls the
effective plan of one linked user into a calendar with source "wieikschedule".
Synced fields are owned by the feed; color, importance, event type and pings
remain local. Writers other than this module must not change mirror rows
except for those local fields (see ``guard_mirror_write``).
"""
import asyncio
import logging
import os
import uuid
from datetime import datetime, timedelta
from urllib.parse import urlsplit

import httpx

from core.database import SessionLocal, CalendarCal, CalendarEvent, WieikCalendarLink, utcnow_naive
from src.secret_storage import decrypt, encrypt

logger = logging.getLogger(__name__)

MANAGED_SOURCE = "wieikschedule"
LOCAL_FIELDS = frozenset({"color", "importance", "event_type"})
SYNC_INTERVAL_SECONDS = 15 * 60
STALE_AFTER = timedelta(hours=1)
READ_ONLY_MESSAGE = (
    "Ten kalendarz to plan zajęć z wieikschedule. Zmień te zajęcia w wieikschedule; "
    "tutaj możesz zmienić tylko kolor, ważność i typ wydarzenia."
)
_NAMESPACE = uuid.UUID("5f0d7a52-8d0c-4d1e-9a39-6c1b8a3e2f10")
_KIND_LABELS = {"laboratory": "laboratorium", "lecture": "wykład", "exercise": "ćwiczenia",
                "project": "projekt", "language": "lektorat"}
_locks: dict[str, asyncio.Lock] = {}


class SyncError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def is_managed(cal) -> bool:
    return bool(cal) and cal.source == MANAGED_SOURCE


def guard_mirror_write(cal, fields=()):
    """Return an error message when a write would change synced mirror fields."""
    if not is_managed(cal):
        return None
    if fields is None or set(fields) - LOCAL_FIELDS:
        return READ_ONLY_MESSAGE
    return None


def mirror_uid(owner, event_id) -> str:
    # calendar_events.uid is a global key: derive it per owner so accounts never collide.
    return str(uuid.uuid5(_NAMESPACE, f"{owner}:{event_id}"))


def gateway_url() -> str:
    """Operator-configured backend address; users never supply a URL to fetch."""
    url = os.getenv("SCHEDULE_GATEWAY_URL", "").strip().rstrip("/")
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.netloc or parts.query or parts.fragment:
        raise SyncError(503, "NOT_CONFIGURED", "Integracja z wieikschedule nie jest skonfigurowana w tym Odysseusie.")
    return url


async def _call(method, path, *, token=None, body=None, etag=None):
    headers = {}
    if token:
        headers["Authorization"] = "Bearer " + token
    if etag:
        headers["If-None-Match"] = etag
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
            response = await client.request(method, gateway_url() + path, headers=headers,
                                            **({"json": body} if body is not None else {}))
    except httpx.HTTPError:
        raise SyncError(503, "UNREACHABLE", "Nie można połączyć się z wieikschedule. Pokazuję ostatni pobrany plan.")
    if response.status_code == 304:
        return response, None
    try:
        data = response.json()
    except ValueError:
        data = {}
    if not response.is_success or not data.get("ok"):
        raise SyncError(response.status_code if response.status_code >= 400 else 503,
                        data.get("error") or "SYNC_FAILED",
                        data.get("message") or "Synchronizacja z wieikschedule nie powiodła się.")
    return response, data


def _occurrence_fields(item):
    day = datetime.strptime(item["date"], "%Y-%m-%d")
    if item["status"] == "day_off":
        start, end, all_day = day, day + timedelta(days=1), True
    else:
        hour, minute, *_ = (int(part) for part in item["start_time"].split(":"))
        start = day.replace(hour=hour, minute=minute)
        end, all_day = start + timedelta(minutes=int(item["duration_min"])), False
    kind = _KIND_LABELS.get(item.get("kind"))
    lines = [line for line in (
        f"Rodzaj: {item['type']}" if item.get("type") else "",
        f"Grupa: {item['group']}" if item.get("group") else "",
        f"Prowadzący: {item['instructor']}" if item.get("instructor") else "",
        "Dopisane do planu (odrabianie)." if item.get("added") else "",
        "Termin zmieniony prywatnie w wieikschedule." if item.get("modified") else "",
        "Zajęcia odwołane prywatnie w wieikschedule." if item["status"] == "cancelled" else "",
        "Plan zajęć z wieikschedule — zmiany wprowadzaj w wieikschedule.",
    ) if line]
    return {
        "summary": f"{item['subject']} ({kind})" if kind else item["subject"],
        "description": "\n".join(lines),
        "location": item.get("room") or "",
        "dtstart": start, "dtend": end, "all_day": all_day, "is_utc": False, "rrule": "",
        "status": "cancelled" if item["status"] == "cancelled" else "confirmed",
    }


def apply_feed(db, cal, owner, payload) -> dict:
    """Bring mirror rows in line with one feed window. Commits are left to the caller."""
    counts = {"created": 0, "updated": 0, "deleted": 0, "unchanged": 0}
    rows = db.query(CalendarEvent).filter(CalendarEvent.calendar_id == cal.id,
                                          CalendarEvent.external_source == MANAGED_SOURCE).all()
    existing = {row.external_id: row for row in rows}
    seen = set()
    for item in payload["events"]:
        event_id = str(item["event_id"])
        seen.add(event_id)
        row = existing.get(event_id)
        if row is not None and row.external_version == item["version"]:
            counts["unchanged"] += 1
            continue
        fields = _occurrence_fields(item)
        if row is None:
            row = CalendarEvent(uid=mirror_uid(owner, event_id), calendar_id=cal.id, origin=MANAGED_SOURCE,
                                external_source=MANAGED_SOURCE, external_id=event_id)
            db.add(row)
            counts["created"] += 1
        else:
            counts["updated"] += 1
        for key, value in fields.items():
            setattr(row, key, value)
        row.external_version = item["version"]
    # Only prune inside the window the feed covered; older history stays.
    window_start = datetime.strptime(payload["date_from"], "%Y-%m-%d")
    window_end = datetime.strptime(payload["date_to"], "%Y-%m-%d") + timedelta(days=1)
    for event_id, row in existing.items():
        if event_id not in seen and window_start <= row.dtstart < window_end:
            db.delete(row)
            counts["deleted"] += 1
    return counts


def _link(db, owner):
    return db.query(WieikCalendarLink).filter(WieikCalendarLink.owner == owner).first()


def status(owner) -> dict:
    with SessionLocal() as db:
        link = _link(db, owner)
        if link is None:
            return {"linked": False}
        stale = not link.last_success_at or utcnow_naive() - link.last_success_at > STALE_AFTER
        return {"linked": True, "calendar_href": link.calendar_id, "label": link.label, "faculty": link.faculty,
                "group_set_name": link.group_set_name,
                "last_success_at": link.last_success_at.isoformat() + "Z" if link.last_success_at else None,
                "last_attempt_at": link.last_attempt_at.isoformat() + "Z" if link.last_attempt_at else None,
                "last_error": link.last_error, "stale": stale}


async def pair(owner, code) -> dict:
    if not isinstance(code, str) or not 1 <= len(code) <= 64:
        raise SyncError(422, "INVALID_CODE", "Wpisz kod połączenia z wieikschedule.")
    with SessionLocal() as db:
        if _link(db, owner):
            raise SyncError(409, "ALREADY_LINKED", "Plan zajęć jest już połączony. Najpierw go rozłącz.")
    _, data = await _call("POST", "/api/calendar-sync/pair", body={"code": code})
    link = data["link"]
    with SessionLocal() as db:
        if _link(db, owner):
            raise SyncError(409, "ALREADY_LINKED", "Plan zajęć jest już połączony. Najpierw go rozłącz.")
        cal = CalendarCal(id=str(uuid.uuid4()), owner=owner, name="Plan zajęć", color="#2e7d32",
                          source=MANAGED_SOURCE, account_id=link["id"])
        db.add(cal)
        db.flush()
        db.add(WieikCalendarLink(owner=owner, calendar_id=cal.id, link_id=link["id"], token=encrypt(data["token"]),
                                 label=link.get("label") or "", faculty=link.get("faculty") or ""))
        db.commit()
    try:
        return await sync_owner(owner)
    except SyncError as error:
        # The link exists now; its status shows why the first pull failed.
        return {"ok": True, "changed": False, "sync_error": error.message}


async def sync_owner(owner) -> dict:
    lock = _locks.setdefault(owner, asyncio.Lock())
    async with lock:
        with SessionLocal() as db:
            link = _link(db, owner)
            if link is None:
                raise SyncError(404, "NOT_LINKED", "Plan zajęć z wieikschedule nie jest połączony.")
            token, etag = decrypt(link.token), link.etag
        attempt = utcnow_naive()
        try:
            response, data = await _call("GET", "/api/calendar-sync/feed", token=token, etag=etag)
        except SyncError as error:
            # Keep the last good mirror; record why it is no longer current.
            with SessionLocal() as db:
                link = _link(db, owner)
                if link is not None:
                    link.last_attempt_at, link.last_error = attempt, error.message
                    db.commit()
            raise
        with SessionLocal() as db:
            link = _link(db, owner)
            if link is None:
                raise SyncError(404, "NOT_LINKED", "Plan zajęć z wieikschedule nie jest połączony.")
            counts = {"created": 0, "updated": 0, "deleted": 0, "unchanged": None}
            if data is not None:
                cal = db.query(CalendarCal).filter(CalendarCal.id == link.calendar_id, CalendarCal.owner == owner).one()
                counts = apply_feed(db, cal, owner, data)
                link.etag = response.headers.get("etag")
                link.group_set_name = data["link"].get("group_set_name")
                link.label = data["link"].get("label") or link.label
            link.last_attempt_at = link.last_success_at = attempt
            link.last_error = None
            db.commit()
        return {"ok": True, "changed": bool(counts["created"] or counts["updated"] or counts["deleted"]), **counts}


async def disconnect(owner) -> dict:
    """Remove the local mirror and revoke this Odysseus' token in wieikschedule."""
    with SessionLocal() as db:
        link = _link(db, owner)
        if link is None:
            raise SyncError(404, "NOT_LINKED", "Plan zajęć z wieikschedule nie jest połączony.")
        token = decrypt(link.token)
    revoked = True
    try:
        await _call("POST", "/api/calendar-sync/unlink", token=token, body={})
    except SyncError as error:
        # An already expired/revoked link is fine; anything else is reported to the user.
        revoked = error.code == "LINK_INVALID"
    with SessionLocal() as db:
        link = _link(db, owner)
        if link is not None:
            cal = db.query(CalendarCal).filter(CalendarCal.id == link.calendar_id, CalendarCal.owner == owner).first()
            db.delete(link)
            db.flush()
            if cal is not None:
                db.delete(cal)
            db.commit()
    return {"ok": True, "revoked": revoked}


async def sync_all() -> None:
    with SessionLocal() as db:
        owners = [row.owner for row in db.query(WieikCalendarLink.owner).all()]
    for owner in owners:
        try:
            await sync_owner(owner)
        except SyncError as error:
            logger.info("wieikschedule calendar sync for one owner failed: %s", error.code)
        except Exception:
            logger.exception("wieikschedule calendar sync failed for one owner")


async def sync_loop() -> None:
    while True:
        await asyncio.sleep(SYNC_INTERVAL_SECONDS)
        try:
            await sync_all()
        except Exception:
            logger.exception("wieikschedule calendar sync loop iteration failed")
