import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';

// package.json has no "type": "module", so load the browser ES module as a
// data: URL instead of importing the .js path.
const source = readFileSync('static/js/buses/logic.js', 'utf8');
const logic = await import('data:text/javascript,' + encodeURIComponent(source));

const stops = [
  {id: 'zabornia', city: 'rabka', lat: 49.608807, lon: 19.927742},
  {id: 'agh', city: 'krakow', lat: 50.064457, lon: 19.924274},
  {id: 'mda', city: 'krakow', lat: 50.067924, lon: 19.949858},
];

function connection(id, times, extra = {}) {
  return {
    id,
    departure: times[0][1],
    arrival: times[times.length - 1][1],
    stops: times.map(([stopId, time]) => ({stop_id: stopId, name: stopId, arrival: time, departure: time, date: '2026-10-27'})),
    ...extra,
  };
}

test('nearest stop picks AGH near the campus and boards towards Rabka', () => {
  const near = logic.nearestStop(stops, {lat: 50.0660, lon: 19.9190});
  assert.equal(near.stop.id, 'agh');
  assert.ok(near.meters > 300 && near.meters < 500, String(near.meters));
  assert.equal(logic.directionFromStop(near.stop), 'to_rabka');
  assert.equal(logic.directionFromStop(stops[0]), 'to_krakow');
});

test('walking time adds a street detour and ignores far-away positions', () => {
  assert.equal(logic.walkMinutes(400), 7);
  assert.equal(logic.walkMinutes(0), 0);
  assert.equal(logic.walkMinutes(logic.MAX_WALK_METERS + 1), 0);
});

test('catchable keeps buses leaving the stop after walk + buffer', () => {
  const connections = [
    connection('a', [['mda', '06:10'], ['agh', '06:15'], ['zabornia', '07:21']]),
    connection('b', [['mda', '06:55'], ['agh', '07:01'], ['zabornia', '08:10']]),
    connection('c', [['mda', '07:40'], ['zabornia', '08:55']]), // skips AGH
  ];
  const now = new Date(2026, 9, 27, 6, 40);

  const list = logic.catchable(connections, {stopId: 'agh', now, walk: 7, buffer: 10, day: '2026-10-27'});

  assert.deepEqual(list.map((item) => item.connection.id), ['b']);
  assert.equal(logic.formatTime(list[0].boarding.at), '07:01');
  assert.equal(logic.formatTime(list[0].leaveBy), '06:44');
  assert.equal(list[0].minutesLeft, 21);
});

test('nobody boards at the terminus', () => {
  const bus = connection('a', [['mda', '06:10'], ['zabornia', '07:21']]);
  assert.equal(logic.boardingAt(bus, 'zabornia', '2026-10-27'), null);
});

test('without stop details only the first boarding stop is known', () => {
  const bus = {id: 'x', departure: '17:09', arrival: '18:20', from_stop_id: 'zabornia', stops: []};
  assert.equal(logic.formatTime(logic.boardingAt(bus, 'zabornia', '2026-10-27').at), '17:09');
  assert.equal(logic.boardingAt(bus, 'agh', '2026-10-27'), null);
  assert.equal(logic.formatTime(logic.arrivalAtEnd(bus, '2026-10-27')), '18:20');
});

test('stop dates carry buses past midnight', () => {
  const bus = connection('late', [['mda', '23:50'], ['zabornia', '00:55']]);
  bus.stops[1].date = '2026-10-28';
  assert.equal(logic.arrivalAtEnd(bus, '2026-10-27').getDate(), 28);
});

test('day helpers', () => {
  assert.equal(logic.addDays('2026-10-31', 1), '2026-11-01');
  assert.equal(logic.addDays('2026-03-29', -1), '2026-03-28');
  assert.equal(logic.formatMinutes(45), '45 min');
  assert.equal(logic.formatMinutes(80), '1 h 20 min');
  assert.equal(logic.formatMinutes(120), '2 h');
});
