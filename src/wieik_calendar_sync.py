"""Managed mirror of the user's wieikschedule plan in the Odysseus calendar.

wieikschedule (Supabase) stays the only source of the plan. Odysseus pulls the
effective plan of one linked user into a calendar with source "wieikschedule".
Synced fields are owned by the feed; color, importance, event type and pings
remain local. A user's own edit of time, length or room is written through to
wieikschedule as a private change (``edit_from_ui``), checked against the
occurrence version last pulled. The agent only prepares previews
(``propose_from_agent``) that the user confirms in the calendar UI. Other
writers must not change synced fields.
"""
import asyncio
import json
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
# Background pulls only; an open calendar refreshes every 3 minutes and own edits apply at once.
SYNC_INTERVAL_SECONDS = 15 * 60
STALE_AFTER = timedelta(hours=1)
READ_ONLY_MESSAGE = (
    "Kalendarz „Plan zajęć” pochodzi z wieikschedule: nie można w nim tworzyć wydarzeń "
    "ani ustawić go jako domyślnego. Zajęcia przesuniesz lub odwołasz, edytując je w kalendarzu."
)
PLAN_FIELDS_MESSAGE = (
    "W planie zajęć zmienisz termin, czas trwania i salę. Tytuł, opis i powtarzanie "
    "pochodzą z wieikschedule; kolor, ważność i typ wydarzenia zmieniasz tutaj."
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
        # Rows mirrored before external_meta existed are rewritten once to gain their flags.
        if row is not None and row.external_version == item["version"] and row.external_meta is not None:
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
        row.external_meta = json.dumps({"added": bool(item.get("added")), "modified": bool(item.get("modified"))})
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
            # A full pull (no ETag) backfills rows that predate external_meta.
            if db.query(CalendarEvent.uid).filter(CalendarEvent.calendar_id == link.calendar_id,
                                                  CalendarEvent.external_source == MANAGED_SOURCE,
                                                  CalendarEvent.external_meta.is_(None)).first():
                etag = None
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


def plan_meta(row) -> dict:
    try:
        meta = json.loads(row.external_meta or "{}")
    except ValueError:
        meta = {}
    return {"added": bool(meta.get("added")), "modified": bool(meta.get("modified")),
            "cancelled": row.status == "cancelled"}


def snapshot(row) -> dict:
    """Detached copy of a mirror row, so network calls run without an open DB session."""
    return {"uid": row.uid, "external_id": row.external_id, "external_version": row.external_version,
            "summary": row.summary or "", "description": row.description or "", "location": row.location or "",
            "dtstart": row.dtstart, "dtend": row.dtend, "all_day": bool(row.all_day), "status": row.status,
            "meta": plan_meta(row)}


def plan_edit(row, changes) -> tuple[dict, dict]:
    """Split an edit of a mirror row into plan values for wieikschedule and local-only fields.

    ``changes`` holds the submitted fields with dtstart/dtend already as local
    (Europe/Warsaw) naive datetimes. Unchanged values that UIs resend are ignored.
    """
    values, local = {}, {}
    for key, value in changes.items():
        if value is None or key in ("dtstart", "dtend", "location"):
            continue
        if key in LOCAL_FIELDS:
            local[key] = value
        elif key in ("summary", "description") and value == row[key]:
            continue
        elif key == "all_day" and bool(value) == row["all_day"]:
            continue
        elif key == "rrule" and not value:
            continue
        else:
            raise SyncError(422, "READ_ONLY_FIELD", PLAN_FIELDS_MESSAGE)
    start, end = changes.get("dtstart") or row["dtstart"], changes.get("dtend") or row["dtend"]
    if (start, end) != (row["dtstart"], row["dtend"]):
        if row["all_day"]:
            raise SyncError(422, "DAY_OFF", "Dni wolnych nie można przesuwać.")
        if end <= start or start.date() != end.date():
            raise SyncError(422, "INVALID_TIME", "Zajęcia muszą zaczynać się i kończyć tego samego dnia.")
        if start.date() != row["dtstart"].date():
            values["date"] = start.strftime("%Y-%m-%d")
        if start.time() != row["dtstart"].time():
            values["start_time"] = start.strftime("%H:%M:00")
        duration = int((end - start).total_seconds() // 60)
        if duration != int((row["dtend"] - row["dtstart"]).total_seconds() // 60):
            values["duration_min"] = duration
    location = changes.get("location")
    if location is not None and location.strip() != row["location"]:
        values["room"] = location.strip()
    return values, local


async def _write(owner, row, operation, values=None, mode="apply") -> dict:
    with SessionLocal() as db:
        link = _link(db, owner)
        if link is None:
            raise SyncError(404, "NOT_LINKED", "Plan zajęć z wieikschedule nie jest połączony.")
        token = decrypt(link.token)
    body = {"event_id": row["external_id"], "operation": operation, "expected_version": row["external_version"],
            "request_id": uuid.uuid4().hex, "mode": mode, **({"values": values} if values else {})}
    try:
        _, data = await _call("POST", "/api/calendar-sync/changes", token=token, body=body)
    except SyncError as error:
        if error.code in ("PLAN_CHANGED", "NOT_IN_PLAN"):
            await _refresh_quietly(owner)
        raise
    if mode == "apply":
        await _refresh_quietly(owner)
    return data


async def _refresh_quietly(owner):
    # The write already succeeded or failed on its own; a failed refresh is recorded in the link status.
    try:
        await sync_owner(owner)
    except SyncError:
        pass


def _mirror_row(owner, uid):
    with SessionLocal() as db:
        row = (db.query(CalendarEvent).join(CalendarCal)
               .filter(CalendarEvent.uid == uid, CalendarCal.owner == owner,
                       CalendarEvent.external_source == MANAGED_SOURCE).first())
        if row is None:
            raise SyncError(404, "NOT_FOUND", "Nie znaleziono zajęć w planie.")
        return snapshot(row)


def _save_local(owner, uid, local):
    if not local:
        return
    with SessionLocal() as db:
        row = (db.query(CalendarEvent).join(CalendarCal)
               .filter(CalendarEvent.uid == uid, CalendarCal.owner == owner).first())
        if row is not None:
            for key, value in local.items():
                setattr(row, key, value or None)
            db.commit()


async def edit_from_ui(owner, uid, changes) -> dict:
    """A user's own edit in the calendar: plan fields go to wieikschedule at once, local ones stay here."""
    row = _mirror_row(owner, uid)
    values, local = plan_edit(row, changes)
    if values:
        await _write(owner, row, "modify", values)
    _save_local(owner, uid, local)
    return {"ok": True, "plan_changed": bool(values)}


async def remove_from_ui(owner, uid) -> dict:
    row = _mirror_row(owner, uid)
    if row["all_day"]:
        raise SyncError(422, "DAY_OFF", "Dni wolnych nie można usuwać.")
    data = await _write(owner, row, "remove")
    return {"ok": True, "result": data.get("result")}


async def restore_from_ui(owner, uid) -> dict:
    row = _mirror_row(owner, uid)
    await _write(owner, row, "restore")
    return {"ok": True}


def cancelled(owner, since) -> list:
    """Privately cancelled classes; hidden from calendar views, listed so they can be restored."""
    with SessionLocal() as db:
        rows = (db.query(CalendarEvent).join(CalendarCal)
                .filter(CalendarCal.owner == owner, CalendarCal.source == MANAGED_SOURCE,
                        CalendarEvent.status == "cancelled", CalendarEvent.dtstart >= since)
                .order_by(CalendarEvent.dtstart).limit(100).all())
        return [{"uid": row.uid, "summary": row.summary, "dtstart": row.dtstart.isoformat(),
                 "location": row.location or ""} for row in rows]


def local_wall_clock(value, is_utc):
    """Mirror rows store Europe/Warsaw wall-clock time; convert parsed UTC instants back to it."""
    from datetime import timezone
    from zoneinfo import ZoneInfo
    if not is_utc:
        return value
    return value.replace(tzinfo=timezone.utc).astimezone(ZoneInfo("Europe/Warsaw")).replace(tzinfo=None)


def _proposal_summary(proposal) -> str:
    before, after = proposal.get("before"), proposal.get("after")
    if not after:
        return "odwołanie zajęć"
    def slot(row):
        start = str(row.get("start_time") or "")[:5]
        return f"{row.get('date')} {start}, sala {row.get('room') or '—'}"
    return f"{slot(before)} → {slot(after)}" if before else f"przywrócenie: {slot(after)}"


async def propose_from_agent(owner, uid, changes=None, remove=False) -> dict:
    """The agent may only prepare a preview; local-only fields are saved directly.

    Nothing here confirms a plan change: confirmation exists only behind the
    signed-in calendar UI routes (``confirm_proposal``).
    """
    row = _mirror_row(owner, uid)
    if remove:
        data = await _write(owner, row, "remove", mode="propose")
        local = {}
    else:
        values, local = plan_edit(row, changes or {})
        data = await _write(owner, row, "modify", values, mode="propose") if values else None
    _save_local(owner, uid, local)
    proposal = (data or {}).get("proposal")
    if not proposal:
        return {"proposal": None, "local": sorted(local)}
    return {"proposal": {"id": proposal["id"], "expires_at": proposal.get("expires_at"),
                         "summary": _proposal_summary(proposal)}, "local": sorted(local)}


def _token(owner):
    with SessionLocal() as db:
        link = _link(db, owner)
        if link is None:
            raise SyncError(404, "NOT_LINKED", "Plan zajęć z wieikschedule nie jest połączony.")
        return decrypt(link.token)


async def list_proposals(owner) -> list:
    _, data = await _call("GET", "/api/calendar-sync/proposals", token=_token(owner))
    return [{"id": item["id"], "subject": item.get("subject"), "type": item.get("type"),
             "summary": _proposal_summary(item), "reason": item.get("reason"), "expires_at": item.get("expires_at")}
            for item in data.get("proposals", [])]


async def confirm_proposal(owner, proposal_id) -> dict:
    """Called only from the signed-in calendar UI after the user clicks confirm."""
    _, data = await _call("POST", "/api/calendar-sync/confirm", token=_token(owner), body={"proposal_id": proposal_id})
    await _refresh_quietly(owner)
    return {"ok": True, "result": data.get("result")}


async def reject_proposal(owner, proposal_id) -> dict:
    await _call("POST", "/api/calendar-sync/reject", token=_token(owner), body={"proposal_id": proposal_id})
    return {"ok": True}
