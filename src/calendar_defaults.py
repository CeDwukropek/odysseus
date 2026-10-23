"""Owner-scoped durable default calendar, shared by HTTP and agent writes."""
from fastapi import HTTPException
from sqlalchemy import func


def default_calendar_id(owner):
    from routes.prefs_routes import _load_for_user
    return (_load_for_user(owner) or {}).get("default_calendar_href") or None


def resolve_calendar(db, owner, reference=None, *, allow_names=False):
    from core.database import CalendarCal
    query = db.query(CalendarCal).filter(CalendarCal.owner == owner)
    if reference:
        if not isinstance(reference, str):
            raise HTTPException(422, "Calendar must be a calendar ID or name.")
        cal = query.filter(CalendarCal.id == reference).first()
        if cal is None and allow_names:
            matches = query.filter(func.lower(CalendarCal.name) == reference.lower()).all()
            if not matches and len(reference) >= 8:
                matches = query.filter(CalendarCal.id.startswith(reference, autoescape=True)).all()
            if len(matches) > 1:
                raise HTTPException(422, "Calendar name is ambiguous; use its ID.")
            cal = matches[0] if matches else None
        if cal is None or cal.owner != owner or (owner is not None and cal.owner is None):
            raise HTTPException(404, "Calendar not found. Select an existing calendar.")
        return cal
    saved = default_calendar_id(owner)
    if saved:
        cal = query.filter(CalendarCal.id == saved).first()
        if cal is None:
            raise HTTPException(409, "Your default calendar is unavailable. Select a new default in Calendar Settings.")
        return cal
    from routes.calendar_routes import _ensure_default_calendar
    return _ensure_default_calendar(db, owner)
