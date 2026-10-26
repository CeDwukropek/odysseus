/**
 * Pure helpers for the bus departures board — no DOM, no fetch, so
 * tests/bus_departures_logic.test.mjs can import them directly.
 */

// 5 km/h, and a straight line is ~30% shorter than the walk along streets.
const WALK_METERS_PER_MINUTE = 5000 / 60;
const STREET_DETOUR = 1.3;
// Past this the user is clearly not walking to the stop (e.g. still at home
// in another town), so only the safety buffer is applied.
export const MAX_WALK_METERS = 5000;

export function distanceMeters(a, b) {
  const rad = (deg) => deg * Math.PI / 180;
  const dLat = rad(b.lat - a.lat);
  const dLon = rad(b.lon - a.lon);
  const h = Math.sin(dLat / 2) ** 2
    + Math.cos(rad(a.lat)) * Math.cos(rad(b.lat)) * Math.sin(dLon / 2) ** 2;
  return 2 * 6371000 * Math.asin(Math.sqrt(h));
}

export function walkMinutes(meters) {
  if (!(meters > 0) || meters > MAX_WALK_METERS) return 0;
  return Math.ceil(meters * STREET_DETOUR / WALK_METERS_PER_MINUTE);
}

export function nearestStop(stops, position) {
  let best = null;
  for (const stop of stops || []) {
    const meters = distanceMeters(position, stop);
    if (!best || meters < best.meters) best = { stop, meters };
  }
  return best;
}

/** Kraków stops board towards Rabka; everything else towards Kraków. */
export function directionFromStop(stop) {
  return stop && stop.city === 'krakow' ? 'to_rabka' : 'to_krakow';
}

/** Local Date for "YYYY-MM-DD" + "HH:MM" in the browser's timezone. */
export function localDate(day, hhmm) {
  const [y, m, d] = String(day).split('-').map(Number);
  const [hh, mm] = String(hhmm).split(':').map(Number);
  return new Date(y, m - 1, d, hh, mm, 0, 0);
}

/**
 * When the connection leaves `stopId`, or null if it does not stop there or
 * `stopId` is its last stop (nobody boards at the terminus).
 */
export function boardingAt(connection, stopId, fallbackDay) {
  const stops = connection.stops || [];
  if (!stops.length) {
    // Details failed to load: only the boarding stop of the search is known.
    if (connection.from_stop_id !== stopId) return null;
    return { stop: null, at: localDate(fallbackDay, connection.departure) };
  }
  const index = stops.findIndex((stop) => stop.stop_id === stopId);
  if (index === -1 || index === stops.length - 1) return null;
  const stop = stops[index];
  return {
    stop,
    at: localDate(stop.date || fallbackDay, stop.departure),
  };
}

/** Arrival at the last stop, for the "→ Kraków (MDA) 06:40" line. */
export function arrivalAtEnd(connection, fallbackDay) {
  const stops = connection.stops || [];
  const last = stops[stops.length - 1];
  if (last) return localDate(last.date || fallbackDay, last.arrival);
  return localDate(fallbackDay, connection.arrival);
}

/**
 * Arrival at `destId` after boarding at `boardId`, or null when the bus does
 * not reach it after that stop. `destId` 'end' (or empty) means the terminus.
 */
export function arrivalAt(connection, destId, boardId, fallbackDay) {
  const stops = connection.stops || [];
  if (!destId || destId === 'end' || !stops.length) {
    return { stop: stops[stops.length - 1] || null, at: arrivalAtEnd(connection, fallbackDay) };
  }
  const from = boardId ? stops.findIndex((stop) => stop.stop_id === boardId) : 0;
  const index = stops.findIndex((stop, i) => i > from && stop.stop_id === destId);
  if (index === -1) return null;
  const stop = stops[index];
  return { stop, at: localDate(stop.date || fallbackDay, stop.arrival) };
}

// A saved place counts as "here" within this radius (plus GPS accuracy,
// capped so a coarse fix can't match a place across town).
export const PLACE_RADIUS_METERS = 250;

/** The saved place the user is at, if any — nearest first. */
export function matchPlace(places, position) {
  if (!position) return null;
  const slack = PLACE_RADIUS_METERS + Math.min(Number(position.accuracy) || 0, 150);
  let best = null;
  for (const place of places || []) {
    const meters = distanceMeters(position, place);
    if (meters <= slack && (!best || meters < best.meters)) best = { place, meters };
  }
  return best;
}

/**
 * Connections the user can still catch at `stopId`: boarding time must be at
 * least walk + buffer minutes from now. `leaveBy` is the latest moment to set
 * off that still keeps the buffer.
 */
export function catchable(connections, { stopId, now, walk = 0, buffer = 0, day }) {
  const marginMs = (walk + buffer) * 60000;
  const out = [];
  for (const connection of connections || []) {
    const boarding = boardingAt(connection, stopId, day);
    if (!boarding) continue;
    if (boarding.at.getTime() - now.getTime() < marginMs) continue;
    out.push({
      connection,
      boarding,
      leaveBy: new Date(boarding.at.getTime() - marginMs),
      minutesLeft: Math.round((boarding.at.getTime() - now.getTime()) / 60000),
    });
  }
  return out.sort((a, b) => a.boarding.at - b.boarding.at);
}

export function isoDay(date) {
  const pad = (n) => String(n).padStart(2, '0');
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}

export function addDays(day, delta) {
  const date = localDate(day, '12:00');
  date.setDate(date.getDate() + delta);
  return isoDay(date);
}

export function formatTime(date) {
  const pad = (n) => String(n).padStart(2, '0');
  return `${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

export function formatMinutes(minutes) {
  if (minutes < 60) return `${minutes} min`;
  const h = Math.floor(minutes / 60);
  const m = minutes % 60;
  return m ? `${h} h ${m} min` : `${h} h`;
}
