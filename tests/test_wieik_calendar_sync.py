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


def test_ui_edits_are_written_to_wieikschedule_and_unsupported_fields_refused(store, monkeypatch):
    apply(store, feed(item('a', version='0123456789abcdef')))
    uid = sync.mirror_uid('alice', 'a')
    calls = []
    ok = (SimpleNamespace(headers={}), {'ok': True, 'status': 'applied'})
    pulled = (SimpleNamespace(headers={'etag': '"v2"'}), feed(item('a', start='12:00:00', modified=True, version='fedcba9876543210')))
    monkeypatch.setattr(sync, '_call', fake_call([ok, pulled], calls))
    create = route('POST', '/events')
    with pytest.raises(HTTPException) as ex:
        asyncio.run(create(request('alice'), routes.EventCreate(summary='X', dtstart='2026-10-19T10:00:00', calendar_href=MIRROR)))
    assert ex.value.status_code == 409
    update = route('PUT', '/events/{uid}')
    # The form resends unchanged fields; times may carry the browser offset (CEST = UTC+2).
    result = asyncio.run(update(request('alice'), uid, routes.EventUpdate(
        summary='Modelowanie układów dynamicznych (laboratorium)', description=mirror_rows(store)['a'].description,
        dtstart='2026-10-19T12:00:00+02:00', dtend='2026-10-19T13:30:00+02:00', all_day=False, rrule='', location='12')))
    assert result == {'ok': True, 'plan_changed': True}
    method, path, token, body, _ = calls[0]
    assert (method, path, token) == ('POST', '/api/calendar-sync/changes', 'wkc_token')
    assert body['operation'] == 'modify' and body['values'] == {'start_time': '12:00:00'}
    assert body['event_id'] == 'a' and body['expected_version'] == '0123456789abcdef' and body['mode'] == 'apply'
    row = mirror_rows(store)['a']
    assert row.dtstart == datetime(2026, 10, 19, 12, 0) and json.loads(row.external_meta)['modified'] is True
    with pytest.raises(HTTPException) as ex:
        asyncio.run(update(request('alice'), uid, routes.EventUpdate(summary='Inny tytuł')))
    assert ex.value.status_code == 422
    assert asyncio.run(update(request('alice'), uid, routes.EventUpdate(color='#00ff00'))) == {'ok': True, 'plan_changed': False}
    assert mirror_rows(store)['a'].color == '#00ff00' and len(calls) == 2
    with pytest.raises(HTTPException) as ex:
        asyncio.run(route('PUT', '/default-calendar')(request('alice', {'calendar_href': MIRROR})))
    assert ex.value.status_code == 409


def test_plan_edit_maps_time_length_and_room_and_ignores_resent_values():
    row = {'summary': 'S', 'description': 'D', 'location': '12', 'all_day': False,
           'dtstart': datetime(2026, 10, 19, 7, 30), 'dtend': datetime(2026, 10, 19, 9, 0)}
    same = {'summary': 'S', 'description': 'D', 'location': '12', 'all_day': False, 'rrule': '',
            'dtstart': row['dtstart'], 'dtend': row['dtend'], 'color': '#fff'}
    assert sync.plan_edit(row, same) == ({}, {'color': '#fff'})
    moved = {'dtstart': datetime(2026, 10, 20, 9, 15), 'dtend': datetime(2026, 10, 20, 11, 15), 'location': ' '}
    assert sync.plan_edit(row, moved)[0] == {'date': '2026-10-20', 'start_time': '09:15:00', 'duration_min': 120, 'room': ''}
    for bad in ({'dtstart': datetime(2026, 10, 19, 23, 0), 'dtend': datetime(2026, 10, 20, 1, 0)},
                {'rrule': 'FREQ=WEEKLY'}, {'all_day': True}, {'description': 'nowy opis'}):
        with pytest.raises(sync.SyncError):
            sync.plan_edit(row, bad)
    assert sync.local_wall_clock(datetime(2026, 10, 19, 10, 0), True) == datetime(2026, 10, 19, 12, 0)
    assert sync.local_wall_clock(datetime(2026, 12, 1, 10, 0), True) == datetime(2026, 12, 1, 11, 0)


def test_delete_cancels_privately_restore_and_conflicts_refresh_the_mirror(store, monkeypatch):
    apply(store, feed(item('a', version='0123456789abcdef')))
    uid = sync.mirror_uid('alice', 'a')
    calls = []
    cancelled = (SimpleNamespace(headers={'etag': '"c"'}), feed(item('a', status='cancelled', version='1111111111111111')))
    restored = (SimpleNamespace(headers={'etag': '"r"'}), feed(item('a', version='2222222222222222')))
    conflict = sync.SyncError(409, 'PLAN_CHANGED', 'Plan zmienił się w międzyczasie.')
    monkeypatch.setattr(sync, '_call', fake_call([
        (SimpleNamespace(headers={}), {'ok': True, 'status': 'applied', 'result': {}}), cancelled,
        (SimpleNamespace(headers={}), {'ok': True, 'status': 'applied'}), restored,
        conflict, restored,
    ], calls))
    asyncio.run(route('DELETE', '/events/{uid}')(request('alice'), uid))
    assert calls[0][3]['operation'] == 'remove'
    assert mirror_rows(store)['a'].status == 'cancelled'
    listed = asyncio.run(route('GET', '/wieik/cancelled')(request('alice')))
    assert [ev['uid'] for ev in listed['events']] == [uid]
    asyncio.run(route('POST', '/wieik/restore')(request('alice', {'uid': uid})))
    assert calls[2][3]['operation'] == 'restore' and calls[2][3]['expected_version'] == '1111111111111111'
    assert mirror_rows(store)['a'].status == 'confirmed'
    with pytest.raises(HTTPException) as ex:
        asyncio.run(route('PUT', '/events/{uid}')(request('alice'), uid, routes.EventUpdate(location='A2')))
    assert ex.value.status_code == 409
    assert calls[-1][1] == '/api/calendar-sync/feed'
    with pytest.raises(HTTPException) as ex:
        asyncio.run(route('POST', '/wieik/restore')(request('bob', {'uid': uid})))
    assert ex.value.status_code == 404


def test_implicit_default_calendar_is_never_the_mirror(store):
    with store() as db:
        db.query(cdb.CalendarCal).filter_by(id='personal').delete()
        db.commit()
        cal = routes._ensure_default_calendar(db, 'alice')
        db.commit()
        assert cal.id != MIRROR and cal.source != sync.MANAGED_SOURCE


def test_agent_only_prepares_previews_and_never_confirms(store, monkeypatch):
    apply(store, feed(item('a', version='0123456789abcdef')))
    uid = sync.mirror_uid('alice', 'a')
    calls = []
    preview = {'id': 'p-1', 'expires_at': 'soon', 'before': {'date': '2026-10-19', 'start_time': '07:30:00', 'room': '12'},
               'after': {'date': '2026-10-20', 'start_time': '07:30:00', 'room': '12'}}
    monkeypatch.setattr(sync, '_call', fake_call([
        (SimpleNamespace(headers={}), {'ok': True, 'status': 'proposed', 'proposal': preview}),
        (SimpleNamespace(headers={}), {'ok': True, 'status': 'proposed', 'proposal': {**preview, 'id': 'p-2', 'after': None}}),
    ], calls))
    call = lambda args: asyncio.run(do_manage_calendar(json.dumps(args), owner='alice'))
    created = call({'action': 'create_event', 'summary': 'X', 'dtstart': '2026-10-19T10:00:00', 'calendar': 'Plan zajęć'})
    assert created['exit_code'] == 1 and 'wieikschedule' in created['error']
    moved = call({'action': 'update_event', 'uid': uid, 'dtstart': '2026-10-20T07:30:00', 'dtend': '2026-10-20T09:00:00'})
    assert moved['exit_code'] == 0 and moved['requires_confirmation'] is True and 'NIE została' in moved['response']
    assert calls[0][3]['mode'] == 'propose' and calls[0][3]['values'] == {'date': '2026-10-20'}
    removed = call({'action': 'delete_event', 'uid': uid})
    assert removed['proposal']['summary'] == 'odwołanie zajęć'
    assert calls[1][3]['operation'] == 'remove' and calls[1][3]['mode'] == 'propose'
    assert all(path != '/api/calendar-sync/confirm' for _, path, *_ in calls)
    assert call({'action': 'update_event', 'uid': uid, 'importance': 'high'})['exit_code'] == 0
    assert call({'action': 'update_event', 'uid': uid, 'summary': 'Nowy tytuł'})['exit_code'] == 1
    assert len(calls) == 2
    row = mirror_rows(store)['a']
    assert row.dtstart == datetime(2026, 10, 19, 7, 30) and row.status == 'confirmed' and row.importance == 'high'


def test_previews_are_listed_confirmed_and_rejected_only_from_the_calendar_ui(store, monkeypatch):
    apply(store, feed(item('a')))
    calls = []
    listed = {'ok': True, 'proposals': [{'id': '11111111-1111-1111-1111-111111111111', 'subject': 'MUD', 'type': 'Lab',
              'before': {'date': '2026-10-19', 'start_time': '07:30:00', 'room': '12'},
              'after': {'date': '2026-10-19', 'start_time': '07:30:00', 'room': 'C3'}, 'reason': None, 'expires_at': 'soon'}]}
    monkeypatch.setattr(sync, '_call', fake_call([
        (SimpleNamespace(headers={}), listed),
        (SimpleNamespace(headers={}), {'ok': True, 'status': 'applied', 'result': {}}),
        (SimpleNamespace(headers={'etag': '"n"'}), feed(item('a', room='C3', modified=True))),
        (SimpleNamespace(headers={}), {'ok': True, 'rejected': True}),
    ], calls))
    pid = '11111111-1111-1111-1111-111111111111'
    result = asyncio.run(route('GET', '/wieik/proposals')(request('alice')))
    assert result['proposals'][0]['summary'] == '2026-10-19 07:30, sala 12 → 2026-10-19 07:30, sala C3'
    asyncio.run(route('POST', '/wieik/proposals/{proposal_id}/confirm')(request('alice'), pid))
    assert calls[1][1:4] == ('/api/calendar-sync/confirm', 'wkc_token', {'proposal_id': pid})
    assert mirror_rows(store)['a'].location == 'C3'
    asyncio.run(route('POST', '/wieik/proposals/{proposal_id}/reject')(request('alice'), pid))
    assert calls[3][1] == '/api/calendar-sync/reject'
    with pytest.raises(HTTPException) as ex:
        asyncio.run(route('POST', '/wieik/proposals/{proposal_id}/confirm')(request('alice'), 'not-a-uuid'))
    assert ex.value.status_code == 422


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


def test_rows_mirrored_before_flags_existed_are_backfilled(store):
    apply(store, feed(item('a', added=True)))
    with store() as db:
        db.query(cdb.CalendarEvent).update({cdb.CalendarEvent.external_meta: None})
        db.commit()
    assert apply(store, feed(item('a', added=True)))['updated'] == 1
    assert json.loads(mirror_rows(store)['a'].external_meta)['added'] is True
    assert apply(store, feed(item('a', added=True)))['unchanged'] == 1


def test_missing_flags_force_a_full_pull_instead_of_304(store, monkeypatch):
    apply(store, feed(item('a')))
    with store() as db:
        db.query(cdb.CalendarEvent).update({cdb.CalendarEvent.external_meta: None})
        db.get(cdb.WieikCalendarLink, 'alice').etag = '"old"'
        db.commit()
    calls = []
    monkeypatch.setattr(sync, '_call', fake_call([(SimpleNamespace(headers={'etag': '"old"'}), feed(item('a')))], calls))
    asyncio.run(sync.sync_owner('alice'))
    assert calls[0][4] is None and mirror_rows(store)['a'].external_meta is not None
