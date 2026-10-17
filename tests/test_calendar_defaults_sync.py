"""Default routing and sync outcomes without a live calendar or model."""
import asyncio
import json
from types import SimpleNamespace
from datetime import datetime
import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
import core.database as cdb
import routes.calendar_routes as routes
import routes.prefs_routes as prefs
from src.calendar_defaults import resolve_calendar
from src.tools.calendar import do_manage_calendar


@pytest.fixture
def store(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'calendar.db'}")
    cdb.Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    monkeypatch.setattr(cdb, 'SessionLocal', sessions)
    monkeypatch.setattr(routes, 'SessionLocal', sessions)
    monkeypatch.setattr(prefs, 'PREFS_FILE', tmp_path / 'prefs.json')
    with sessions() as db:
        db.add_all([
            cdb.CalendarCal(id='local', owner='alice', name='Personal', source='local'),
            cdb.CalendarCal(id='remote', owner='alice', name='Google', source='caldav'),
            cdb.CalendarCal(id='bob', owner='bob', name='Google', source='caldav'),
        ])
        db.commit()
    yield sessions
    engine.dispose()


def route(method, path):
    return next(r.endpoint for r in routes.setup_calendar_routes().routes
                if method in r.methods and r.path == '/api/calendar' + path)


def request(owner, body=None):
    async def parse(): return body
    return SimpleNamespace(state=SimpleNamespace(current_user=owner), json=parse)


def test_default_is_durable_and_owner_scoped(store):
    result = asyncio.run(route('PUT', '/default-calendar')(request('alice', {'calendar_href': 'remote'})))
    assert result['default_calendar_href'] == 'remote'
    with store() as db:
        assert resolve_calendar(db, 'alice').id == 'remote'
        assert resolve_calendar(db, 'alice', 'local').id == 'local'
        with pytest.raises(HTTPException) as ex: resolve_calendar(db, 'bob', 'remote')
        assert ex.value.status_code == 404
    listed = asyncio.run(route('GET', '/calendars')(request('alice')))
    assert [c['href'] for c in listed['calendars'] if c['is_default']] == ['remote']
    with pytest.raises(HTTPException):
        asyncio.run(route('PUT', '/default-calendar')(request('alice', {'calendar_href': 'bob'})))


def test_invalid_explicit_or_stale_default_does_not_silently_fall_back(store):
    prefs._save_for_user('alice', {'default_calendar_href': 'removed'})
    with store() as db:
        with pytest.raises(HTTPException) as ex: resolve_calendar(db, 'alice')
        assert ex.value.status_code == 409
        with pytest.raises(HTTPException): resolve_calendar(db, 'alice', 'typo', allow_names=True)
        assert resolve_calendar(db, 'alice', 'Google', allow_names=True).id == 'remote'


def test_agent_uses_default_and_reports_pending_without_claiming_remote_success(store, monkeypatch):
    prefs._save_for_user('alice', {'default_calendar_href': 'remote'})
    import src.caldav_sync as sync
    async def fail(owner, uid): return {'ok': False, 'error': 'offline'}
    monkeypatch.setattr(sync, 'push_event_create', fail)
    result = asyncio.run(do_manage_calendar(json.dumps({
        'action': 'create_event', 'summary': 'Test', 'dtstart': '2026-10-20T10:00:00Z'
    }), owner='alice'))
    assert result['exit_code'] == 0
    assert result['calendar_href'] == 'remote'
    assert result['sync']['status'] == 'pending'
    assert 'pending' in result['response']
    with store() as db:
        ev=db.query(cdb.CalendarEvent).one()
        assert ev.caldav_sync_pending == 'create'


def test_http_default_and_sync_success(store, monkeypatch):
    prefs._save_for_user('alice', {'default_calendar_href': 'remote'})
    import src.caldav_sync as sync
    async def success(owner, uid): return {'ok': True}
    monkeypatch.setattr(sync, 'push_event_create', success)
    result = asyncio.run(route('POST', '/events')(
        request('alice'), routes.EventCreate(summary='Test', dtstart='2026-10-20T10:00:00Z')))
    assert result['calendar_href'] == 'remote'
    assert result['sync']['status'] == 'synced'


def test_retry_pending_before_pull(monkeypatch):
    import src.caldav_sync as sync
    calls=[]
    async def push(owner): calls.append('push'); return {'events': 1, 'errors': []}
    async def pull(owner): calls.append('pull'); return {'events': 1, 'errors': []}
    monkeypatch.setattr(sync,'push_pending_events',push)
    monkeypatch.setattr(sync,'sync_caldav',pull)
    asyncio.run(sync.sync_caldav_direction('alice','both'))
    assert calls == ['push','pull']


def test_google_empty_discovery_opens_events_and_lookup_failure_is_not_absence():
    from src.caldav_writeback import _discover_calendars,push_event,_stable_cal_id
    from caldav.lib.error import AuthorizationError
    url='https://apidata.googleusercontent.com/caldav/v2/test@example.com/user'
    client=SimpleNamespace(url=url,principal=lambda:SimpleNamespace(calendars=lambda:[]),
                           calendar=lambda url:SimpleNamespace(url=url))
    assert str(_discover_calendars(client)[0].url).endswith('/events')
    class Remote:
        url='https://dav.example/calendar/'
        def event_by_uid(self,uid): raise AuthorizationError('denied')
        def save_event(self,data): pytest.fail('must not recreate after lookup failure')
    with pytest.raises(AuthorizationError):
        push_event([Remote()], _stable_cal_id(Remote.url), {'uid':'test'})


def test_remote_update_preserves_attendees_alarms_and_detached_occurrences():
    from src.caldav_writeback import merge_remote_event, build_event_ical
    from icalendar import Calendar
    original = "\r\n".join([
        "BEGIN:VCALENDAR", "VERSION:2.0", "BEGIN:VEVENT", "UID:series",
        "SUMMARY:Old", "DTSTART:20261019T090000Z", "DTEND:20261019T100000Z",
        "ORGANIZER:mailto:owner@example.com", "ATTENDEE:mailto:guest@example.com",
        "EXDATE:20261026T090000Z", "X-CUSTOM:keep", "BEGIN:VALARM",
        "ACTION:DISPLAY", "TRIGGER:-PT10M", "DESCRIPTION:Reminder", "END:VALARM",
        "END:VEVENT", "BEGIN:VEVENT", "UID:series", "RECURRENCE-ID:20261102T090000Z",
        "DTSTART:20261102T110000Z", "DTEND:20261102T120000Z", "SUMMARY:Exception",
        "END:VEVENT", "END:VCALENDAR", ""
    ])
    replacement = build_event_ical({
        'uid':'series', 'summary':'New', 'dtstart':datetime(2026,10,19,9),
        'dtend':datetime(2026,10,19,10), 'is_utc':True,
        'recurrence_exdates':['2026-11-09T09:00']
    })
    result = Calendar.from_ical(merge_remote_event(original,replacement,'series'))
    events=result.walk('VEVENT');master=events[0]
    assert len(events)==2
    assert str(master['SUMMARY'])=='New'
    assert str(master['ATTENDEE'])=='mailto:guest@example.com'
    assert master.get('ORGANIZER')
    assert str(master['X-CUSTOM'])=='keep'
    assert master.walk('VALARM')
    assert len(master.get('EXDATE'))==2
    assert str(events[1]['SUMMARY'])=='Exception'


def test_malformed_remote_calendar_is_not_overwritten():
    from src.caldav_writeback import merge_remote_event
    with pytest.raises(ValueError): merge_remote_event('invalid','invalid','event')
