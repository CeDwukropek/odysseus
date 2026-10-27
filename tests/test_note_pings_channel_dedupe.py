"""The background note scanner must not erase the reminder channel.

dispatch_reminder records {"at", "channel"} in note_pings_<owner>.json and
skips a second email/ntfy send for the same note only when the recorded
channel matches. The scanner used to overwrite that entry with a bare
timestamp, so when it fired first an open tab's frontend poller sent the same
reminder again a minute later (two "Reminder (Odysseus)" emails).
"""

import asyncio
import json
from datetime import datetime, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import core.database as cd
import routes.note_routes as note_routes
import src.builtin_actions as builtin_actions


def _db_with_due_note(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    cd.Note.__table__.create(engine)
    session_factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    db = session_factory()
    due = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    db.add(cd.Note(id="note-1", owner="admin", title="Sprawozdanie", content="Oddać", due_date=due))
    db.commit()
    db.close()
    monkeypatch.setattr(cd, "SessionLocal", session_factory)


def test_scanner_keeps_the_channel_dispatch_recorded(tmp_path, monkeypatch):
    _db_with_due_note(monkeypatch)
    monkeypatch.setattr(builtin_actions, "DATA_DIR", str(tmp_path))
    state = tmp_path / "note_pings_admin.json"

    async def fake_dispatch(*, title, note_body, note_id, owner):
        # What the real dispatch_reminder writes after a successful email.
        state.write_text(json.dumps({note_id: {"at": datetime.now(timezone.utc).isoformat(), "channel": "email"}}))
        return {"email_sent": True}

    monkeypatch.setattr(note_routes, "dispatch_reminder", fake_dispatch)

    asyncio.run(builtin_actions.action_ping_notes(owner="admin"))

    assert json.loads(state.read_text())["note-1"]["channel"] == "email"


def test_frontend_fire_after_scanner_is_skipped(tmp_path, monkeypatch):
    monkeypatch.setattr(note_routes, "DATA_DIR", str(tmp_path))
    (tmp_path / "note_pings_admin.json").write_text(json.dumps(
        {"note-1": {"at": datetime.now(timezone.utc).isoformat(), "channel": "email"}}
    ))

    result = asyncio.run(note_routes.dispatch_reminder(
        title="Sprawozdanie", note_body="Oddać", note_id="note-1", owner="admin",
        settings_override={"reminder_channel": "email"},
    ))

    assert result.get("skipped") is True
    assert result["email_sent"] is False
