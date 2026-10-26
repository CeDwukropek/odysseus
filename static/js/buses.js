/**
 * Buses Module — Rabka-Zdrój ↔ Kraków departures board.
 *
 * "Zdążę" lists today's buses the user can still catch from the nearest (or
 * chosen) stop; "Cały dzień" shows a whole day with expandable stop lists.
 * Data comes from /api/buses/schedule, which answers `loading` while the
 * backend pages through e-podroznik, so the board polls until it is ready.
 *
 * Saved places ("Dom → Zabornia, 12 min") live in the per-user prefs store so
 * phone and desktop share them; "time to leave" alerts are sent by the server
 * through ntfy, with a browser notification as well while the board is open.
 */

import uiModule from './ui.js';
import { makeWindowDraggable } from './windowDrag.js';
import {
  MAX_WALK_METERS,
  addDays,
  arrivalAt,
  boardingAt,
  catchable,
  directionFromStop,
  formatMinutes,
  formatTime,
  isoDay,
  localDate,
  matchPlace,
  nearestStop,
  walkMinutes,
} from './buses/logic.js';

const API_BASE = window.location.origin;
const esc = uiModule.esc;
const POLL_MS = 3000;
const CLOCK_MS = 15000;
const DEFAULT_BUFFER = 10;
const PREF_KEY = 'odysseus-buses-prefs';
const PLACES_PREF = 'bus_places';
const DIRECTION_LABELS = { to_krakow: '→ Kraków', to_rabka: '→ Rabka' };
const PIN_ICON = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px"><path d="M20 10c0 6-8 12-8 12s-8-6-8-12a8 8 0 0 1 16 0Z"/><circle cx="12" cy="10" r="3"/></svg>';
const HOME_ICON = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px"><path d="M3 10.5 12 3l9 7.5"/><path d="M5 9.5V21h14V9.5"/></svg>';
const BELL_ICON = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9"/><path d="M10.3 21a1.94 1.94 0 0 0 3.4 0"/></svg>';

const BUS_ICON = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px;margin-right:6px"><path d="M8 6v6"/><path d="M15 6v6"/><path d="M2 12h19.6"/><path d="M18 18h3s.5-1.7.8-2.8c.1-.4.2-.8.2-1.2 0-.4-.1-.8-.2-1.2l-1.4-5C20.1 6.8 19.1 6 18 6H4a2 2 0 0 0-2 2v10h3"/><circle cx="7" cy="18" r="2"/><path d="M9 18h5"/><circle cx="16" cy="18" r="2"/></svg>';

let _open = false;
let _escHandler = null;
let _clockInterval = null;
let _pollTimer = null;
let _stops = [];
let _tab = 'catch';
let _day = isoDay(new Date());
let _position = null;        // { lat, lon, accuracy }
let _locationError = '';
let _schedules = new Map();  // "direction|day" → response
let _expanded = new Set();   // connection ids with the stop list open
let _places = [];            // [{ id, name, lat, lon, stop_id, walk }]
let _placesOpen = false;
let _alerts = new Map();     // alert id → alert, as stored by the server
let _pushReady = false;
let _firedLocally = new Set();
let _prefs = _loadPrefs();

function _loadPrefs() {
  const defaults = { stop: 'auto', direction: 'auto', buffer: DEFAULT_BUFFER, dest: {} };
  try {
    const saved = JSON.parse(localStorage.getItem(PREF_KEY) || '{}');
    const merged = { ...defaults, ...(saved && typeof saved === 'object' ? saved : {}) };
    if (!merged.dest || typeof merged.dest !== 'object') merged.dest = {};
    return merged;
  } catch (_) {
    return defaults;
  }
}

function _savePrefs() {
  try { localStorage.setItem(PREF_KEY, JSON.stringify(_prefs)); } catch (_) {}
}

// ---- Derived state ----

function _stopById(id) {
  return _stops.find((s) => s.id === id) || null;
}

function _currentPlace() {
  if (_prefs.stop !== 'auto') return null;
  const match = matchPlace(_places, _position);
  return match && _stopById(match.place.stop_id) ? match.place : null;
}

function _boardingStop() {
  if (_prefs.stop !== 'auto') {
    const stop = _stopById(_prefs.stop);
    return stop ? { stop, meters: null } : null;
  }
  const place = _currentPlace();
  if (place) return { stop: _stopById(place.stop_id), meters: null, place };
  if (!_position) return null;
  return nearestStop(_stops, _position);
}

function _direction() {
  if (_prefs.direction !== 'auto') return _prefs.direction;
  const boarding = _boardingStop();
  return boarding ? directionFromStop(boarding.stop) : 'to_krakow';
}

function _destId() {
  return _prefs.dest[_direction()] || 'end';
}

function _walk() {
  const boarding = _boardingStop();
  if (!boarding) return 0;
  if (boarding.place) return Number(boarding.place.walk) || 0;
  return boarding.meters != null ? walkMinutes(boarding.meters) : 0;
}

function _key(direction, day) { return `${direction}|${day}`; }

function _alertId(day, connectionId, stopId) { return `${day}-${connectionId}-${stopId}`; }

// ---- Data ----

async function _fetchJson(url, options = {}) {
  try {
    const res = await fetch(url, { credentials: 'same-origin', ...options });
    const body = await res.json().catch(() => ({}));
    return { ok: res.ok, status: res.status, body };
  } catch (_) {
    return { ok: false, status: 0, body: {} };
  }
}

async function _fetchSchedule(direction, day, { refresh = false } = {}) {
  const params = new URLSearchParams({ direction, date: day });
  if (refresh) params.set('refresh', '1');
  const { ok, status, body } = await _fetchJson(`${API_BASE}/api/buses/schedule?${params}`);
  if (status === 0) return { status: 'error', error: 'Brak połączenia z serwerem.' };
  if (!ok) return { status: 'error', error: body.detail || `Błąd ${status}` };
  return body;
}

/** Load (and keep polling) the schedule the active tab needs. */
async function _load({ refresh = false } = {}) {
  clearTimeout(_pollTimer);
  _pollTimer = null;
  const direction = _direction();
  const day = _tab === 'catch' ? isoDay(new Date()) : _day;
  const key = _key(direction, day);
  const result = await _fetchSchedule(direction, day, { refresh });
  if (!_open) return;
  const previous = _schedules.get(key);
  // Keep showing the last good copy if a refresh attempt fails.
  if (!(result.status === 'error' && previous && previous.schedule)) _schedules.set(key, result);
  _renderBody();
  if (result.status === 'loading' || result.refreshing) {
    _pollTimer = setTimeout(() => { if (_open) _load(); }, POLL_MS);
  }
}

async function _loadStops() {
  if (_stops.length) return;
  const { ok, body } = await _fetchJson(`${API_BASE}/api/buses/stops`);
  if (ok) _stops = body.stops || [];
}

async function _loadPlaces() {
  const { ok, body } = await _fetchJson(`${API_BASE}/api/prefs/${PLACES_PREF}`);
  _places = ok && Array.isArray(body.value) ? body.value : [];
}

async function _savePlaces() {
  const { ok } = await _fetchJson(`${API_BASE}/api/prefs/${PLACES_PREF}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ value: _places }),
  });
  if (!ok) uiModule.showError('Nie udało się zapisać miejsc.');
}

async function _loadAlerts() {
  const { ok, body } = await _fetchJson(`${API_BASE}/api/buses/alerts`);
  if (!ok) return;
  _alerts = new Map((body.alerts || []).map((a) => [a.id, a]));
  _pushReady = !!body.push_ready;
}

function _locate() {
  if (!('geolocation' in navigator)) {
    _locationError = 'Ta przeglądarka nie udostępnia lokalizacji.';
    _render();
    return;
  }
  _locationError = '';
  navigator.geolocation.getCurrentPosition(
    (pos) => {
      const before = _direction();
      _position = { lat: pos.coords.latitude, lon: pos.coords.longitude, accuracy: pos.coords.accuracy };
      if (!_open) return;
      _render();
      if (_direction() !== before) _load();
    },
    (err) => {
      _locationError = err && err.code === 1
        ? 'Brak zgody na lokalizację — wybierz przystanek ręcznie.'
        : 'Nie udało się ustalić lokalizacji — wybierz przystanek ręcznie.';
      if (_open) _render();
    },
    { enableHighAccuracy: true, timeout: 15000, maximumAge: 60000 },
  );
}

// ---- Alerts ----

async function _toggleAlert(card) {
  const { alertId, connectionId, stopId, leaveAt, title, body } = card.dataset;
  if (_alerts.has(alertId)) {
    const { ok } = await _fetchJson(`${API_BASE}/api/buses/alerts/${encodeURIComponent(alertId)}`, { method: 'DELETE' });
    if (ok || !_alerts.has(alertId)) _alerts.delete(alertId);
    _renderBody();
    return;
  }
  const { ok, body: response } = await _fetchJson(`${API_BASE}/api/buses/alerts`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      date: isoDay(new Date()), connection_id: connectionId, stop_id: stopId,
      leave_at: leaveAt, title, body,
    }),
  });
  if (!ok) {
    uiModule.showError(response.detail || 'Nie udało się ustawić przypomnienia.');
    return;
  }
  _alerts.set(response.alert.id, response.alert);
  _pushReady = !!response.push_ready;
  const when = formatTime(new Date(response.alert.leave_at));
  if (_pushReady) uiModule.showToast(`Przypomnę o ${when} (ntfy).`);
  else uiModule.showToast(`Przypomnienie na ${when} zapisane, ale ntfy nie jest skonfigurowane — powiadomię tylko przy otwartej karcie.`, { duration: 7000 });
  if ('Notification' in window && Notification.permission === 'default') Notification.requestPermission();
  _renderBody();
}

/** Browser-side "leave now" while the board is open (the server sends the ntfy push). */
function _fireLocalAlerts() {
  const now = Date.now();
  for (const alert of _alerts.values()) {
    if (_firedLocally.has(alert.id) || new Date(alert.leave_at).getTime() > now) continue;
    _firedLocally.add(alert.id);
    _alerts.delete(alert.id);
    try {
      if ('Notification' in window && Notification.permission === 'granted') {
        new Notification(alert.title, { body: alert.body, tag: `bus-${alert.id}` });
      }
    } catch (_) {}
    uiModule.showToast(`${alert.title} — ${alert.body}`, { duration: 15000 });
  }
}

// ---- Rendering ----

/** "Kraków (AGH)" → "AGH", for compact per-row times. */
function _shortStopName(stopId) {
  const stop = _stopById(stopId);
  const label = stop ? stop.label : stopId;
  const match = /\(([^)]+)\)$/.exec(label);
  return match ? match[1] : label;
}

function _stopOptions(selected, { first }) {
  return [first]
    .concat(_stops.map((s) => `<option value="${esc(s.id)}"${s.id === selected ? ' selected' : ''}>${esc(s.label)}</option>`))
    .join('');
}

function _locationHtml(boarding) {
  if (_prefs.stop !== 'auto') {
    return boarding ? `Przystanek: <b>${esc(boarding.stop.label)}</b> · bez dojścia, tylko zapas` : '';
  }
  if (!boarding) return esc(_locationError || 'Ustalam lokalizację…');
  if (boarding.place) {
    return `Jesteś w: <b>${esc(boarding.place.name)}</b> → ${esc(boarding.stop.label)} · ${Number(boarding.place.walk) || 0} min (Twoje miejsce)`;
  }
  const meters = boarding.meters;
  const far = meters > MAX_WALK_METERS;
  const distance = meters >= 1000 ? `${(meters / 1000).toFixed(1)} km` : `${Math.round(meters)} m`;
  return `Najbliżej: <b>${esc(boarding.stop.label)}</b> · ${distance}`
    + (far ? ' · daleko, doliczam tylko zapas' : ` · ~${walkMinutes(meters)} min pieszo`);
}

function _placesHtml() {
  if (!_placesOpen) return '';
  const rows = _places.map((place) => `
    <li><span><b>${esc(place.name)}</b> → ${esc((_stopById(place.stop_id) || {}).label || place.stop_id)} · ${Number(place.walk) || 0} min</span>
      <button type="button" class="buses-icon-btn" data-place-delete="${esc(place.id)}" title="Usuń" aria-label="Usuń ${esc(place.name)}">✕</button></li>`).join('');
  let form;
  if (!_position) {
    form = '<div class="buses-muted">Żeby zapisać miejsce, najpierw ustal lokalizację (pinezka).</div>';
  } else {
    const near = nearestStop(_stops, _position);
    const suggested = near ? walkMinutes(near.meters) || 1 : 5;
    form = `
      <div class="buses-place-form">
        <input id="buses-place-name" type="text" maxlength="40" placeholder="Nazwa, np. Dom">
        <select id="buses-place-stop">${_stopOptions(near && near.stop.id, { first: '' })}</select>
        <label class="buses-field"><input id="buses-place-walk" type="number" min="0" max="120" value="${suggested}"> min</label>
        <button type="button" class="buses-link-btn" id="buses-place-save">Zapisz to miejsce</button>
      </div>`;
  }
  return `
    <div class="buses-places">
      <div class="buses-muted">Moje miejsca: gdy jesteś w promieniu ~250 m, liczę Twój czas dojścia zamiast szacunku.</div>
      ${rows ? `<ul class="buses-place-list">${rows}</ul>` : ''}
      ${form}
    </div>`;
}

function _controlsHtml() {
  const boarding = _boardingStop();
  const direction = _direction();
  const directionButtons = ['auto', 'to_krakow', 'to_rabka'].map((value) => {
    const label = value === 'auto' ? 'Auto' : DIRECTION_LABELS[value];
    const active = _prefs.direction === value ? ' active' : '';
    return `<button type="button" class="buses-seg-btn${active}" data-direction="${value}">${label}</button>`;
  }).join('');
  const boardSelected = _stopById(_prefs.stop) ? _prefs.stop : 'auto';

  return `
    <div class="buses-controls">
      <div class="buses-seg" role="group" aria-label="Kierunek">${directionButtons}</div>
      <label class="buses-field">Wsiadam
        <select id="buses-stop">${_stopOptions(boardSelected, { first: '<option value="auto">Najbliższy (GPS)</option>' })}</select>
      </label>
      <label class="buses-field">Wysiadam
        <select id="buses-dest">${_stopOptions(_destId(), { first: '<option value="end">Koniec trasy</option>' })}</select>
      </label>
      <label class="buses-field">Zapas
        <input id="buses-buffer" type="number" min="0" max="120" step="1" value="${Number(_prefs.buffer) || 0}"> min
      </label>
      <button type="button" class="buses-icon-btn${_placesOpen ? ' active' : ''}" id="buses-places" title="Moje miejsca" aria-label="Moje miejsca">${HOME_ICON}</button>
      <button type="button" class="buses-icon-btn" id="buses-locate" title="Odśwież lokalizację" aria-label="Odśwież lokalizację">${PIN_ICON}</button>
      <button type="button" class="buses-icon-btn" id="buses-refresh" title="Pobierz rozkład ponownie" aria-label="Pobierz rozkład ponownie">↻</button>
    </div>
    <div class="buses-location">${_locationHtml(boarding)} <span class="buses-muted">· ${esc(DIRECTION_LABELS[direction])}</span></div>
    ${_placesHtml()}
  `;
}

function _statusHtml(result) {
  if (!result) return '<div class="buses-empty">Ładowanie…</div>';
  if (result.status === 'loading') {
    return '<div class="buses-empty">Pobieram rozkład z e-podroznik.pl…<br><span class="buses-muted">Pierwsze pobranie dnia trwa ok. 30 s.</span></div>';
  }
  if (result.status === 'error') return `<div class="buses-empty buses-error">${esc(result.error || 'Błąd')}</div>`;
  return '';
}

function _stopsHtml(connection, highlightIds) {
  const stops = connection.stops || [];
  if (!stops.length) return '<div class="buses-stops buses-muted">Brak szczegółów trasy.</div>';
  const items = stops.map((stop, index) => {
    const time = index === 0 || stop.arrival === stop.departure
      ? stop.departure
      : `${stop.arrival}–${stop.departure}`;
    const mark = stop.stop_id && highlightIds.includes(stop.stop_id) ? 'buses-stop-here' : '';
    return `<li class="${mark}"><span class="buses-stop-time">${esc(index === stops.length - 1 ? stop.arrival : time)}</span><span>${esc(stop.name)}</span></li>`;
  }).join('');
  return `<ol class="buses-stops">${items}</ol>`;
}

function _footerHtml(result) {
  const schedule = result && result.schedule;
  if (!schedule) return '';
  const fetched = schedule.fetched_at ? new Date(schedule.fetched_at) : null;
  const when = fetched && !Number.isNaN(fetched.getTime()) ? formatTime(fetched) : '?';
  return `<div class="buses-footer">Rozkład z e-podroznik.pl, pobrany ${when}${result.refreshing ? ' · odświeżam…' : ''}. Godziny rozkładowe, bez opóźnień.</div>`;
}

function _destLabel(arrival, connection) {
  if (arrival && arrival.stop && arrival.stop.stop_id && _destId() !== 'end') {
    return (_stopById(arrival.stop.stop_id) || {}).label || arrival.stop.name;
  }
  return connection.to_label;
}

function _renderCatch(body) {
  const direction = _direction();
  const today = isoDay(new Date());
  const result = _schedules.get(_key(direction, today));
  const status = _statusHtml(result);
  const boarding = _boardingStop();
  if (status) { body.innerHTML = status; return; }
  if (!boarding) {
    body.innerHTML = '<div class="buses-empty">Wybierz przystanek albo zezwól na lokalizację.</div>' + _footerHtml(result);
    return;
  }

  const buffer = Number(_prefs.buffer) || 0;
  const walk = _walk();
  const destId = _destId();
  const list = catchable(result.schedule.connections, {
    stopId: boarding.stop.id, now: new Date(), walk, buffer, day: today,
  })
    .map((item) => ({ ...item, arrival: arrivalAt(item.connection, destId, boarding.stop.id, today) }))
    .filter((item) => item.arrival);
  if (!list.length) {
    const dest = destId !== 'end' ? ` do ${(_stopById(destId) || {}).label || destId}` : '';
    body.innerHTML = `
      <div class="buses-empty">Dziś nie ma już busa ${esc(DIRECTION_LABELS[direction])} z przystanku ${esc(boarding.stop.label)}${esc(dest)}, na który zdążysz.
        <br><button type="button" class="buses-link-btn" id="buses-tomorrow">Pokaż jutrzejszy rozkład</button></div>
      ${_footerHtml(result)}`;
    return;
  }

  const cards = list.map(({ connection, boarding: board, leaveBy, minutesLeft, arrival }) => {
    const open = _expanded.has(connection.id);
    const destLabel = _destLabel(arrival, connection);
    const alertId = _alertId(today, connection.id, boarding.stop.id);
    const alertOn = _alerts.has(alertId);
    const leaveAt = alertOn ? new Date(_alerts.get(alertId).leave_at) : leaveBy;
    const alertTitle = `Wyjdź na busa ${formatTime(board.at)}`;
    const alertBody = `${connection.carrier} z ${boarding.stop.label} o ${formatTime(board.at)} → ${destLabel} ${formatTime(arrival.at)}. Dojście ${walk} min + zapas ${buffer} min.`;
    return `
      <div class="buses-card${open ? ' open' : ''}" data-id="${esc(connection.id)}"
           data-alert-id="${esc(alertId)}" data-connection-id="${esc(connection.id)}" data-stop-id="${esc(boarding.stop.id)}"
           data-leave-at="${esc(leaveBy.toISOString())}" data-title="${esc(alertTitle)}" data-body="${esc(alertBody)}">
        <div class="buses-card-head">
          <button type="button" class="buses-card-main" aria-expanded="${open}">
            <span class="buses-card-time">${formatTime(board.at)}</span>
            <span class="buses-card-info">
              <span class="buses-card-line"><b>${esc(connection.carrier)}</b> · ${esc(destLabel)} ${formatTime(arrival.at)}</span>
              <span class="buses-card-sub">za ${formatMinutes(minutesLeft)} · wyjdź najpóźniej ${formatTime(leaveAt)}</span>
            </span>
            <span class="buses-chevron">▾</span>
          </button>
          <button type="button" class="buses-bell${alertOn ? ' active' : ''}" aria-pressed="${alertOn}"
                  title="${alertOn ? 'Usuń przypomnienie' : `Przypomnij o ${formatTime(leaveAt)}`}"
                  aria-label="${alertOn ? 'Usuń przypomnienie' : `Przypomnij o ${formatTime(leaveAt)}`}">${BELL_ICON}</button>
        </div>
        ${open ? _stopsHtml(connection, [boarding.stop.id, arrival.stop && arrival.stop.stop_id].filter(Boolean)) : ''}
      </div>`;
  }).join('');
  body.innerHTML = `<div class="buses-cards">${cards}</div>${_footerHtml(result)}`;
}

function _renderDay(body) {
  const direction = _direction();
  const result = _schedules.get(_key(direction, _day));
  const today = isoDay(new Date());
  const longDay = localDate(_day, '12:00').toLocaleDateString('pl-PL', { weekday: 'long', day: 'numeric', month: 'long' });
  const dayLabel = longDay.charAt(0).toUpperCase() + longDay.slice(1);
  const nav = `
    <div class="buses-daynav">
      <button type="button" class="buses-icon-btn" data-day-shift="-1" title="Poprzedni dzień">‹</button>
      <span class="buses-daylabel">${esc(dayLabel)}${_day === today ? ' (dziś)' : ''}</span>
      <button type="button" class="buses-icon-btn" data-day-shift="1" title="Następny dzień">›</button>
      ${_day !== today ? '<button type="button" class="buses-link-btn" data-day-today>dziś</button>' : ''}
    </div>`;
  const status = _statusHtml(result);
  if (status) { body.innerHTML = nav + status; return; }

  const connections = result.schedule.connections || [];
  if (!connections.length) {
    body.innerHTML = nav + '<div class="buses-empty">Brak kursów Maxbus, Majerbus i Szwagropol w tym dniu.</div>' + _footerHtml(result);
    return;
  }
  const boarding = _boardingStop();
  const boardId = boarding && boarding.stop.id;
  const destId = _destId();
  const now = new Date();
  const rows = connections.map((connection) => {
    const open = _expanded.has(connection.id);
    const departed = localDate(_day, connection.departure) < now;
    const atStop = boardId ? boardingAt(connection, boardId, _day) : null;
    const parts = [];
    if (atStop && atStop.stop && atStop.stop.stop_id !== connection.from_stop_id) {
      parts.push(`${esc(_shortStopName(boardId))} ${formatTime(atStop.at)}`);
    }
    if (destId !== 'end') {
      const arrival = arrivalAt(connection, destId, atStop && atStop.stop ? boardId : null, _day);
      if (arrival) parts.push(`→ ${esc(_shortStopName(destId))} ${formatTime(arrival.at)}`);
    }
    const extra = parts.length ? `<span class="buses-muted"> · ${parts.join(' ')}</span>` : '';
    return `
      <div class="buses-row${open ? ' open' : ''}${departed ? ' departed' : ''}" data-id="${esc(connection.id)}">
        <button type="button" class="buses-row-main" aria-expanded="${open}">
          <span class="buses-row-time">${esc(connection.departure)}</span>
          <span class="buses-row-route">${esc(connection.from_label)} → ${esc(connection.to_label)} ${esc(connection.arrival)}${extra}</span>
          <span class="buses-row-carrier">${esc(connection.carrier)}</span>
          <span class="buses-chevron">▾</span>
        </button>
        ${open ? _stopsHtml(connection, [boardId, destId].filter(Boolean)) : ''}
      </div>`;
  }).join('');
  body.innerHTML = nav + `<div class="buses-table">${rows}</div>` + _footerHtml(result);
}

/** Tabs + list only — safe to call from timers without closing an open <select>. */
function _renderBody() {
  const modal = document.getElementById('buses-modal');
  if (!modal) return;
  modal.querySelectorAll('.buses-tab').forEach((btn) => {
    const active = btn.dataset.tab === _tab;
    btn.classList.toggle('active', active);
    btn.setAttribute('aria-selected', String(active));
  });
  const body = modal.querySelector('.buses-body');
  if (_tab === 'catch') _renderCatch(body);
  else _renderDay(body);
}

function _render() {
  const modal = document.getElementById('buses-modal');
  if (!modal) return;
  modal.querySelector('.buses-controls-wrap').innerHTML = _controlsHtml();
  _renderBody();
}

// ---- Events ----

function _onClick(e) {
  const target = e.target.closest('button');
  if (!target) return;
  if (target.classList.contains('buses-tab')) {
    _tab = target.dataset.tab;
    _renderBody();
    _load();
  } else if (target.dataset.direction) {
    _prefs.direction = target.dataset.direction;
    _savePrefs();
    _render();
    _load();
  } else if (target.id === 'buses-locate') {
    _locate();
  } else if (target.id === 'buses-refresh') {
    _load({ refresh: true });
  } else if (target.id === 'buses-places') {
    _placesOpen = !_placesOpen;
    _render();
  } else if (target.id === 'buses-place-save') {
    _addPlace();
  } else if (target.dataset.placeDelete) {
    _places = _places.filter((p) => p.id !== target.dataset.placeDelete);
    _savePlaces();
    _render();
    _load();
  } else if (target.id === 'buses-tomorrow') {
    _tab = 'day';
    _day = addDays(isoDay(new Date()), 1);
    _render();
    _load();
  } else if (target.dataset.dayShift) {
    _day = addDays(_day, Number(target.dataset.dayShift));
    _render();
    _load();
  } else if ('dayToday' in target.dataset) {
    _day = isoDay(new Date());
    _render();
    _load();
  } else if (target.classList.contains('buses-bell')) {
    _toggleAlert(target.closest('.buses-card'));
  } else if (target.classList.contains('buses-card-main') || target.classList.contains('buses-row-main')) {
    const id = target.closest('[data-id]').dataset.id;
    if (_expanded.has(id)) _expanded.delete(id);
    else _expanded.add(id);
    _renderBody();
  }
}

function _addPlace() {
  const name = (document.getElementById('buses-place-name')?.value || '').trim();
  const stopId = document.getElementById('buses-place-stop')?.value;
  const walk = Math.max(0, Math.min(120, Math.round(Number(document.getElementById('buses-place-walk')?.value) || 0)));
  if (!name || !_stopById(stopId) || !_position) {
    uiModule.showError('Podaj nazwę i przystanek.');
    return;
  }
  _places = _places.concat({
    id: Date.now().toString(36),
    name,
    lat: Number(_position.lat.toFixed(5)),
    lon: Number(_position.lon.toFixed(5)),
    stop_id: stopId,
    walk,
  });
  _savePlaces();
  _render();
  _load();
}

function _onChange(e) {
  if (e.target.id === 'buses-stop') {
    _prefs.stop = e.target.value;
    _savePrefs();
    if (_prefs.stop === 'auto' && !_position) _locate();
    _render();
    _load();
  } else if (e.target.id === 'buses-dest') {
    _prefs.dest = { ..._prefs.dest, [_direction()]: e.target.value };
    _savePrefs();
    _renderBody();
  } else if (e.target.id === 'buses-buffer') {
    const value = Math.max(0, Math.min(120, Math.round(Number(e.target.value) || 0)));
    _prefs.buffer = value;
    _savePrefs();
    e.target.value = String(value);
    _renderBody();
  }
}

// ---- Modal ----

export async function openBuses() {
  if (_open) return;
  _open = true;
  _tab = 'catch';
  _day = isoDay(new Date());
  _expanded = new Set();
  _placesOpen = false;

  const modal = document.createElement('div');
  modal.className = 'modal';
  modal.id = 'buses-modal';
  modal.innerHTML = `
    <div class="modal-content buses-modal-content">
      <div class="modal-header">
        <h4 style="position:relative;top:-2px;">${BUS_ICON}Busy Rabka ↔ Kraków</h4>
        <span style="flex:1"></span>
        <button class="close-btn" id="buses-close">✖</button>
      </div>
      <div class="memory-tabs buses-tabs" role="tablist">
        <button class="memory-tab buses-tab active" data-tab="catch" role="tab" aria-selected="true">Zdążę</button>
        <button class="memory-tab buses-tab" data-tab="day" role="tab" aria-selected="false">Cały dzień</button>
      </div>
      <div class="buses-controls-wrap"></div>
      <div class="modal-body buses-body"></div>
    </div>
  `;
  document.body.appendChild(modal);

  const content = modal.querySelector('.modal-content');
  const header = modal.querySelector('.modal-header');
  if (content && header) makeWindowDraggable(modal, { content, header });

  document.getElementById('buses-close').addEventListener('click', closeBuses);
  content.addEventListener('click', _onClick);
  content.addEventListener('change', _onChange);
  modal.addEventListener('click', (e) => {
    if (uiModule.isTouchInsideModal()) return;
    if (e.target === modal) closeBuses();
  });
  _escHandler = (e) => { if (e.key === 'Escape') closeBuses(); };
  document.addEventListener('keydown', _escHandler);
  // Re-evaluate "za N min" / which buses are still catchable as time passes.
  _clockInterval = setInterval(() => {
    if (!_open) return;
    _fireLocalAlerts();
    _renderBody();
  }, CLOCK_MS);

  _render();
  await Promise.all([_loadStops(), _loadPlaces(), _loadAlerts()]);
  if (!_open) return;
  if (_prefs.stop === 'auto') _locate();
  _render();
  _load();
}

export function closeBuses() {
  if (!_open) return;
  _open = false;
  clearTimeout(_pollTimer);
  _pollTimer = null;
  clearInterval(_clockInterval);
  _clockInterval = null;
  if (_escHandler) {
    document.removeEventListener('keydown', _escHandler);
    _escHandler = null;
  }
  const modal = document.getElementById('buses-modal');
  if (modal) {
    const content = modal.querySelector('.modal-content');
    if (content) {
      content.classList.add('modal-closing');
      content.addEventListener('animationend', () => modal.remove(), { once: true });
      setTimeout(() => { if (modal.parentElement) modal.remove(); }, 250);
    } else {
      modal.remove();
    }
  }
}

export function isBusesOpen() { return _open; }

const busesModule = { openBuses, closeBuses, isBusesOpen };
export default busesModule;
