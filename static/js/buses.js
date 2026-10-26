/**
 * Buses Module — Rabka-Zdrój ↔ Kraków departures board.
 *
 * "Zdążę" lists today's buses the user can still catch from the nearest (or
 * chosen) stop; "Cały dzień" shows a whole day with expandable stop lists.
 * Data comes from /api/buses/schedule, which answers `loading` while the
 * backend pages through e-podroznik, so the board polls until it is ready.
 */

import uiModule from './ui.js';
import { makeWindowDraggable } from './windowDrag.js';
import {
  MAX_WALK_METERS,
  addDays,
  arrivalAtEnd,
  boardingAt,
  catchable,
  directionFromStop,
  formatMinutes,
  formatTime,
  isoDay,
  localDate,
  nearestStop,
  walkMinutes,
} from './buses/logic.js';

const API_BASE = window.location.origin;
const esc = uiModule.esc;
const POLL_MS = 3000;
const CLOCK_MS = 30000;
const DEFAULT_BUFFER = 10;
const PREF_KEY = 'odysseus-buses-prefs';
const DIRECTION_LABELS = { to_krakow: '→ Kraków', to_rabka: '→ Rabka' };
const PIN_ICON = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px"><path d="M20 10c0 6-8 12-8 12s-8-6-8-12a8 8 0 0 1 16 0Z"/><circle cx="12" cy="10" r="3"/></svg>';

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
let _prefs = _loadPrefs();

function _loadPrefs() {
  const defaults = { stop: 'auto', direction: 'auto', buffer: DEFAULT_BUFFER };
  try {
    const saved = JSON.parse(localStorage.getItem(PREF_KEY) || '{}');
    return { ...defaults, ...(saved && typeof saved === 'object' ? saved : {}) };
  } catch (_) {
    return defaults;
  }
}

function _savePrefs() {
  try { localStorage.setItem(PREF_KEY, JSON.stringify(_prefs)); } catch (_) {}
}

// ---- Derived state ----

function _boardingStop() {
  if (_prefs.stop !== 'auto') {
    const stop = _stops.find((s) => s.id === _prefs.stop);
    return stop ? { stop, meters: null } : null;
  }
  if (!_position) return null;
  return nearestStop(_stops, _position);
}

function _direction() {
  if (_prefs.direction !== 'auto') return _prefs.direction;
  const boarding = _boardingStop();
  return boarding ? directionFromStop(boarding.stop) : 'to_krakow';
}

function _walk() {
  const boarding = _boardingStop();
  return boarding && boarding.meters != null ? walkMinutes(boarding.meters) : 0;
}

function _key(direction, day) { return `${direction}|${day}`; }

// ---- Data ----

async function _fetchSchedule(direction, day, { refresh = false } = {}) {
  const params = new URLSearchParams({ direction, date: day });
  if (refresh) params.set('refresh', '1');
  try {
    const res = await fetch(`${API_BASE}/api/buses/schedule?${params}`, { credentials: 'same-origin' });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) return { status: 'error', error: body.detail || `Błąd ${res.status}` };
    return body;
  } catch (_) {
    return { status: 'error', error: 'Brak połączenia z serwerem.' };
  }
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
  try {
    const res = await fetch(`${API_BASE}/api/buses/stops`, { credentials: 'same-origin' });
    if (res.ok) _stops = (await res.json()).stops || [];
  } catch (_) {}
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

// ---- Rendering ----

/** "Kraków (AGH)" → "AGH", for the compact per-row boarding time. */
function _shortStopName(stopId) {
  const stop = _stops.find((s) => s.id === stopId);
  const label = stop ? stop.label : stopId;
  const match = /\(([^)]+)\)$/.exec(label);
  return match ? match[1] : label;
}

function _controlsHtml() {
  const boarding = _boardingStop();
  const stopOptions = ['<option value="auto">Najbliższy (GPS)</option>']
    .concat(_stops.map((s) => `<option value="${esc(s.id)}">${esc(s.label)}</option>`))
    .join('');
  const directionButtons = ['auto', 'to_krakow', 'to_rabka'].map((value) => {
    const label = value === 'auto' ? 'Auto' : DIRECTION_LABELS[value];
    const active = _prefs.direction === value ? ' active' : '';
    return `<button type="button" class="buses-seg-btn${active}" data-direction="${value}">${label}</button>`;
  }).join('');

  let location = '';
  if (_prefs.stop === 'auto') {
    if (boarding) {
      const meters = boarding.meters;
      const far = meters > MAX_WALK_METERS;
      const distance = meters >= 1000 ? `${(meters / 1000).toFixed(1)} km` : `${Math.round(meters)} m`;
      location = `Najbliżej: <b>${esc(boarding.stop.label)}</b> · ${distance}`
        + (far ? ' · daleko, doliczam tylko zapas' : ` · ~${walkMinutes(meters)} min pieszo`);
    } else {
      location = esc(_locationError || 'Ustalam lokalizację…');
    }
  } else if (boarding) {
    location = `Przystanek: <b>${esc(boarding.stop.label)}</b> · bez dojścia, tylko zapas`;
  }

  return `
    <div class="buses-controls">
      <div class="buses-seg" role="group" aria-label="Kierunek">${directionButtons}</div>
      <label class="buses-field">Przystanek
        <select id="buses-stop">${stopOptions}</select>
      </label>
      <label class="buses-field">Zapas
        <input id="buses-buffer" type="number" min="0" max="120" step="1" value="${Number(_prefs.buffer) || 0}"> min
      </label>
      <button type="button" class="buses-icon-btn" id="buses-locate" title="Odśwież lokalizację" aria-label="Odśwież lokalizację">${PIN_ICON}</button>
      <button type="button" class="buses-icon-btn" id="buses-refresh" title="Pobierz rozkład ponownie" aria-label="Pobierz rozkład ponownie">↻</button>
    </div>
    <div class="buses-location">${location}</div>
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

function _stopsHtml(connection, highlightId) {
  const stops = connection.stops || [];
  if (!stops.length) return '<div class="buses-stops buses-muted">Brak szczegółów trasy.</div>';
  const items = stops.map((stop, index) => {
    const time = index === 0 || stop.arrival === stop.departure
      ? stop.departure
      : `${stop.arrival}–${stop.departure}`;
    const mark = stop.stop_id && stop.stop_id === highlightId ? ' buses-stop-here' : '';
    return `<li class="${mark.trim()}"><span class="buses-stop-time">${esc(index === stops.length - 1 ? stop.arrival : time)}</span><span>${esc(stop.name)}</span></li>`;
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
  const list = catchable(result.schedule.connections, {
    stopId: boarding.stop.id, now: new Date(), walk: _walk(), buffer, day: today,
  });
  if (!list.length) {
    body.innerHTML = `
      <div class="buses-empty">Dziś nie ma już busa ${esc(DIRECTION_LABELS[direction])} z przystanku ${esc(boarding.stop.label)}, na który zdążysz.
        <br><button type="button" class="buses-link-btn" id="buses-tomorrow">Pokaż jutrzejszy rozkład</button></div>
      ${_footerHtml(result)}`;
    return;
  }

  const cards = list.map(({ connection, boarding: board, leaveBy, minutesLeft }) => {
    const open = _expanded.has(connection.id);
    const arrival = formatTime(arrivalAtEnd(connection, today));
    return `
      <div class="buses-card${open ? ' open' : ''}" data-id="${esc(connection.id)}">
        <button type="button" class="buses-card-main" aria-expanded="${open}">
          <span class="buses-card-time">${formatTime(board.at)}</span>
          <span class="buses-card-info">
            <span class="buses-card-line"><b>${esc(connection.carrier)}</b> · ${esc(connection.to_label)} ${arrival}</span>
            <span class="buses-card-sub">za ${formatMinutes(minutesLeft)} · wyjdź najpóźniej ${formatTime(leaveBy)}</span>
          </span>
          <span class="buses-chevron">▾</span>
        </button>
        ${open ? _stopsHtml(connection, boarding.stop.id) : ''}
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
  const now = new Date();
  const rows = connections.map((connection) => {
    const open = _expanded.has(connection.id);
    const departed = localDate(_day, connection.departure) < now;
    const atStop = boarding ? boardingAt(connection, boarding.stop.id, _day) : null;
    const stopTime = atStop && atStop.stop && atStop.stop.stop_id !== connection.from_stop_id
      ? `<span class="buses-muted"> · ${esc(_shortStopName(boarding.stop.id))} ${formatTime(atStop.at)}</span>`
      : '';
    return `
      <div class="buses-row${open ? ' open' : ''}${departed ? ' departed' : ''}" data-id="${esc(connection.id)}">
        <button type="button" class="buses-row-main" aria-expanded="${open}">
          <span class="buses-row-time">${esc(connection.departure)}</span>
          <span class="buses-row-route">${esc(connection.from_label)} → ${esc(connection.to_label)} ${esc(connection.arrival)}${stopTime}</span>
          <span class="buses-row-carrier">${esc(connection.carrier)}</span>
          <span class="buses-chevron">▾</span>
        </button>
        ${open ? _stopsHtml(connection, boarding && boarding.stop.id) : ''}
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
  const controls = modal.querySelector('.buses-controls-wrap');
  controls.innerHTML = _controlsHtml();
  const select = controls.querySelector('#buses-stop');
  if (select) select.value = _stops.some((s) => s.id === _prefs.stop) ? _prefs.stop : 'auto';
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
  } else if (target.classList.contains('buses-card-main') || target.classList.contains('buses-row-main')) {
    const id = target.parentElement.dataset.id;
    if (_expanded.has(id)) _expanded.delete(id);
    else _expanded.add(id);
    _renderBody();
  }
}

function _onChange(e) {
  if (e.target.id === 'buses-stop') {
    _prefs.stop = e.target.value;
    _savePrefs();
    if (_prefs.stop === 'auto' && !_position) _locate();
    _render();
    _load();
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
  _clockInterval = setInterval(() => { if (_open) _renderBody(); }, CLOCK_MS);

  _render();
  await _loadStops();
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
