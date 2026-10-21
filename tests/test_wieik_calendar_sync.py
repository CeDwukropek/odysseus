"""wieikschedule plan mirror: feed application, read-only guards and link lifecycle, without a network."""
import asyncio
import json
from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import core.database as cdb
import routes.calendar_routes as routes
import routes.prefs_routes as prefs
import src.builtin_actions as actions
import src.wieik_calendar_sync as sync
from src.tools.calendar import do_manage_calendar

MIRROR = 'mirror'


def item(event_id, date='2026-10-19', start='07:30:00', status='scheduled', version=None, **extra):
    data = {'event_id': event_id, 'date': date, 'start_time': start, 'duration_min': 90,
            'subject': 'Modelowanie układów dynamicznych', 'type': 'Laboratorium', 'kind': 'laboratory',
            'group': 'L1', 'room': '12', 'instructor': 'Test Instructor', 'status': status,
            'added': False, 'modified': False, 'override_revision': 0, **extra}
    data['version'] = version or f'{event_id}-{date}-{start}-{status}-{extra}'
    return data


def feed(*events, date_from='2026-10-01', date_to='2026-10-31'):
    return {'events': list(events), 'date_from': date_from, 'date_to': date_to,
            'link': {'id': 'link-1', 'label': 'Odysseus', 'group_set_name': 'Mój plan'}}


@pytest.fixture
def store(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'calendar.db'}")
    cdb.Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    for module in (cdb, routes, sync):
        monkeypatch.setattr(module, 'SessionLocal', sessions)
    monkeypatch.setattr(prefs, 'PREFS_FILE', tmp_path / 'prefs.json')
    monkeypatch.setattr(actions, 'TIDY_CALENDAR_STATE_FILE', str(tmp_path / 'tidy.json'))
    monkeypatch.setattr(sync, 'encrypt', lambda value: 'enc:' + value)
    monkeypatch.setattr(sync, 'decrypt', lambda value: value[4:])
    with sessions() as db:
        db.add_all([
            cdb.CalendarCal(id=MIRROR, owner='alice', name='Plan zajęć', source=sync.MANAGED_SOURCE),
            cdb.CalendarCal(id='personal', owner='alice', name='Personal', source='local'),
            cdb.CalendarCal(id='bob', owner='bob', name='Personal', source='local'),
        ])
        db.add(cdb.WieikCalendarLink(owner='alice', calendar_id=MIRROR, link_id='link-1',
                                     token='enc:wkc_token', label='Odysseus', faculty='EiAs3'))
        db.commit()
    yield sessions
    engine.dispose()


def apply(store, payload):
    with store() as db:
        cal = db.get(cdb.CalendarCal, MIRROR)
        counts = sync.apply_feed(db, cal, 'alice', payload)
        db.commit()
    return counts


def mirror_rows(store):
    with store() as db:
        return {row.external_id: SimpleNamespace(**{c.name: getattr(row, c.name) for c in row.__table__.columns})
                for row in db.query(cdb.CalendarEvent).filter(cdb.CalendarEvent.calendar_id == MIRROR)}


def route(method, path):
    return next(r.endpoint for r in routes.setup_calendar_routes().routes
                if method in r.methods and r.path == '/api/calendar' + path)


def request(owner, body=None):
    async def parse(): return body
    return SimpleNamespace(state=SimpleNamespace(current_user=owner), json=parse)


def test_feed_creates_occurrences_with_local_wall_clock_and_owner_scoped_uids(store):
    counts = apply(store, feed(item('a'), item('cancelled', status='cancelled'),
                               item('free', date='2026-10-20', status='day_off', subject='Dzień wolny', kind='other')))
    assert counts == {'created': 3, 'updated': 0, 'deleted': 0, 'unchanged': 0}
    rows = mirror_rows(store)
    assert rows['a'].dtstart == datetime(2026, 10, 19, 7, 30) and rows['a'].dtend == datetime(2026, 10, 19, 9, 0)
    assert rows['a'].is_utc is False and rows['a'].location == '12' and rows['a'].status == 'confirmed'
    assert rows['a'].summary == 'Modelowanie układów dynamicznych (laboratorium)'
    assert rows['a'].uid == sync.mirror_uid('alice', 'a') != sync.mirror_uid('bob', 'a')
    assert rows['cancelled'].status == 'cancelled'
    assert rows['free'].all_day is True and rows['free'].dtend == datetime(2026, 10, 21)


def test_resync_updates_synced_fields_keeps_local_ones_and_prunes_only_inside_window(store):
    apply(store, feed(item('a'), item('gone'), item('old', date='2026-09-10')))
    with store() as db:
        row = db.query(cdb.CalendarEvent).filter_by(external_id='a').one()
        row.color, row.importance, row.event_type = '#ff0000', 'high', 'study'
        db.commit()
    assert apply(store, feed(item('a'))) == {'created': 0, 'updated': 0, 'deleted': 1, 'unchanged': 1}
    counts = apply(store, feed(item('a', start='12:00:00', modified=True, version='v2')))
    assert counts == {'created': 0, 'updated': 1, 'deleted': 0, 'unchanged': 0}
    rows = mirror_rows(store)
    assert rows['a'].dtstart == datetime(2026, 10, 19, 12, 0)
    assert 'zmieniony prywatnie' in rows['a'].description
    assert (rows['a'].color, rows['a'].importance, rows['a'].event_type) == ('#ff0000', 'high', 'study')
    assert 'gone' not in rows and 'old' in rows


def test_guard_allows_only_local_fields_on_managed_calendars():
    managed, local = SimpleNamespace(source=sync.MANAGED_SOURCE), SimpleNamespace(source='local')
    assert sync.guard_mirror_write(managed, {'color'}) is None
    assert sync.guard_mirror_write(managed, {'color', 'dtstart'}) == sync.READ_ONLY_MESSAGE
    assert sync.guard_mirror_write(managed, None) == sync.READ_ONLY_MESSAGE
    assert sync.guard_mirror_write(local, {'dtstart'}) is None


def test_ui_routes_refuse_plan_changes_but_allow_color(store):
    apply(store, feed(item('a')))
    uid = sync.mirror_uid('alice', 'a')
    create = route('POST', '/events')
    with pytest.raises(HTTPException) as ex:
        asyncio.run(create(request('alice'), routes.EventCreate(summary='X', dtstart='2026-10-19T10:00:00', calendar_href=MIRROR)))
    assert ex.value.status_code == 409
    update = route('PUT', '/events/{uid}')
    with pytest.raises(HTTPException) as ex:
        asyncio.run(update(request('alice'), uid, routes.EventUpdate(dtstart='2026-10-20T07:30:00')))
    assert ex.value.status_code == 409 and 'wieikschedule' in ex.value.detail
    assert asyncio.run(update(request('alice'), uid, routes.EventUpdate(color='#00ff00')))['ok']
    with pytest.raises(HTTPException) as ex:
        asyncio.run(route('DELETE', '/events/{uid}')(request('alice'), uid))
    assert ex.value.status_code == 409
    assert mirror_rows(store)['a'].dtstart == datetime(2026, 10, 19, 7, 30)
    with pytest.raises(HTTPException) as ex:
        asyncio.run(route('PUT', '/default-calendar')(request('alice', {'calendar_href': MIRROR})))
    assert ex.value.status_code == 409


def test_implicit_default_calendar_is_never_the_mirror(store):
    with store() as db:
        db.query(cdb.CalendarCal).filter_by(id='personal').delete()
        db.commit()
        cal = routes._ensure_default_calendar(db, 'alice')
        db.commit()
        assert cal.id != MIRROR and cal.source != sync.MANAGED_SOURCE


def test_agent_tool_cannot_change_plan_fields_of_the_mirror(store):
    apply(store, feed(item('a')))
    uid = sync.mirror_uid('alice', 'a')
    call = lambda args: asyncio.run(do_manage_calendar(json.dumps(args), owner='alice'))
    created = call({'action': 'create_event', 'summary': 'X', 'dtstart': '2026-10-19T10:00:00', 'calendar': 'Plan zajęć'})
    assert created['exit_code'] == 1 and 'wieikschedule' in created['error']
    assert call({'action': 'update_event', 'uid': uid, 'dtstart': '2026-10-20T07:30:00'})['exit_code'] == 1
    assert call({'action': 'delete_event', 'uid': uid})['exit_code'] == 1
    assert call({'action': 'update_event', 'uid': uid, 'importance': 'high'})['exit_code'] == 0
    row = mirror_rows(store)['a']
    assert row.dtstart == datetime(2026, 10, 19, 7, 30) and row.importance == 'high'


def test_tidy_calendar_is_owner_scoped_and_skips_the_mirror(store):
    apply(store, feed(item('a')))
    start = datetime(2026, 10, 19, 7, 30)
    with store() as db:
        db.add_all([
            cdb.CalendarEvent(uid='alice-copy', calendar_id='personal', summary='Modelowanie układów dynamicznych (laboratorium)', dtstart=start, dtend=start),
            cdb.CalendarEvent(uid='bob-1', calendar_id='bob', summary='Spotkanie', dtstart=start, dtend=start),
            cdb.CalendarEvent(uid='bob-2', calendar_id='bob', summary='Spotkanie', dtstart=start, dtend=start),
        ])
        db.commit()
    asyncio.run(actions.action_tidy_calendar('alice'))
    with store() as db:
        uids = {row.uid for row in db.query(cdb.CalendarEvent)}
    assert {'alice-copy', 'bob-1', 'bob-2', sync.mirror_uid('alice', 'a')} <= uids
    asyncio.run(actions.action_tidy_calendar('bob'))
    with store() as db:
        assert db.query(cdb.CalendarEvent).filter_by(calendar_id='bob').count() == 1


def fake_call(responses, calls):
    async def call(method, path, *, token=None, body=None, etag=None):
        calls.append((method, path, token, body, etag))
        result = responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result
    return call


def test_sync_applies_feed_honours_304_and_keeps_mirror_on_failure(store, monkeypatch):
    calls = []
    ok = SimpleNamespace(headers={'etag': '"v1"'})
    monkeypatch.setattr(sync, '_call', fake_call([
        (ok, feed(item('a'))),
        (SimpleNamespace(headers={}), None),
        sync.SyncError(401, 'LINK_INVALID', 'Połączenie kalendarza jest nieważne.'),
    ], calls))
    first = asyncio.run(sync.sync_owner('alice'))
    assert first['created'] == 1 and first['changed'] is True
    assert calls[0][2] == 'wkc_token' and calls[0][4] is None
    assert asyncio.run(sync.sync_owner('alice'))['changed'] is False
    assert calls[1][4] == '"v1"'
    with pytest.raises(sync.SyncError):
        asyncio.run(sync.sync_owner('alice'))
    status = sync.status('alice')
    assert status['last_error'] == 'Połączenie kalendarza jest nieważne.' and status['group_set_name'] == 'Mój plan'
    assert 'a' in mirror_rows(store)


def test_pair_creates_one_managed_calendar_and_disconnect_revokes(store, monkeypatch):
    calls = []
    with store() as db:
        db.query(cdb.WieikCalendarLink).delete()
        db.query(cdb.CalendarCal).filter_by(id=MIRROR).delete()
        db.commit()
    paired = (SimpleNamespace(headers={}), {'ok': True, 'token': 'wkc_new',
                                            'link': {'id': 'link-2', 'label': 'Odysseus', 'faculty': 'EiAs3'}})
    monkeypatch.setattr(sync, '_call', fake_call([paired, (SimpleNamespace(headers={'etag': '"v"'}), feed(item('a'))),
                                                  (SimpleNamespace(headers={}), {'ok': True})], calls))
    assert asyncio.run(sync.pair('alice', 'ABCD-EFGH-JKMN'))['created'] == 1
    with store() as db:
        link = db.get(cdb.WieikCalendarLink, 'alice')
        assert link.token == 'enc:wkc_new'
        assert db.get(cdb.CalendarCal, link.calendar_id).source == sync.MANAGED_SOURCE
    with pytest.raises(sync.SyncError) as ex:
        asyncio.run(sync.pair('alice', 'ABCD-EFGH-JKMN'))
    assert ex.value.code == 'ALREADY_LINKED'
    assert asyncio.run(sync.disconnect('alice')) == {'ok': True, 'revoked': True}
    assert calls[-1][:3] == ('POST', '/api/calendar-sync/unlink', 'wkc_new')
    assert sync.status('alice') == {'linked': False}
    with store() as db:
        assert db.query(cdb.CalendarEvent).filter_by(external_source=sync.MANAGED_SOURCE).count() == 0


def test_gateway_url_is_operator_configured_only(monkeypatch):
    monkeypatch.delenv('SCHEDULE_GATEWAY_URL', raising=False)
    with pytest.raises(sync.SyncError) as ex:
        sync.gateway_url()
    assert ex.value.code == 'NOT_CONFIGURED'
    monkeypatch.setenv('SCHEDULE_GATEWAY_URL', 'file:///etc/passwd')
    with pytest.raises(sync.SyncError):
        sync.gateway_url()
    monkeypatch.setenv('SCHEDULE_GATEWAY_URL', 'http://app:3100/')
    assert sync.gateway_url() == 'http://app:3100'


def test_pairing_succeeds_even_when_the_first_pull_fails(store, monkeypatch):
    with store() as db:
        db.query(cdb.WieikCalendarLink).delete()
        db.query(cdb.CalendarCal).filter_by(id=MIRROR).delete()
        db.commit()
    paired = (SimpleNamespace(headers={}), {'ok': True, 'token': 'wkc_new',
                                            'link': {'id': 'link-3', 'label': 'Odysseus', 'faculty': 'EiAs3'}})
    missing = sync.SyncError(409, 'GROUP_SET_MISSING', 'Zestaw grup wybrany dla kalendarza nie istnieje.')
    monkeypatch.setattr(sync, '_call', fake_call([paired, missing], []))
    result = asyncio.run(sync.pair('alice', 'ABCD-EFGH-JKMN'))
    assert result['ok'] is True and result['sync_error'] == missing.message
    status = sync.status('alice')
    assert status['linked'] is True and status['last_error'] == missing.message and status['last_success_at'] is None
