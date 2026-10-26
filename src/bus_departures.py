"""Rabka-Zdrój ↔ Kraków departures board backed by e-podroznik.pl.

A full day takes e-podroznik ~20 s to page through plus one details request
per connection, so the board serves a disk-backed cache and refreshes it in a
background thread while the UI polls.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import date as date_type
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urljoin

import requests

from src.tools.bus_schedule import (
    BASE_URL,
    BUS_CARRIER_TYPES,
    LOCAL_TIMEZONE,
    REQUEST_TIMEOUT,
    SEARCH_PATH,
    BusScheduleError,
    _fold,
    extend_search_results,
    initialize_session,
    parse_connection_rows,
    parse_route_details,
    resolve_place,
    search_connections,
)

logger = logging.getLogger(__name__)

DIRECTIONS: dict[str, tuple[str, str]] = {
    "to_krakow": ("Rabka-Zdrój", "Kraków"),
    "to_rabka": ("Kraków", "Rabka-Zdrój"),
}

# Stops of the Zakopianka corridor in Rabka → Kraków order. Coordinates come
# from e-podroznik's route maps; `match` lists folded substrings of the many
# spellings carriers use ("Kraków AGH d/w", "Kraków AGH/UR", "Kraków Agh").
STOPS: list[dict[str, Any]] = [
    {"id": "zabornia", "label": "Rabka (Zabornia)", "city": "rabka",
     "lat": 49.608807, "lon": 19.927742, "match": ("zabornia",)},
    {"id": "skomielna", "label": "Skomielna Biała", "city": "rabka",
     "lat": 49.644409, "lon": 19.913952, "match": ("skomielna",)},
    {"id": "myslenice", "label": "Myślenice (Estakada)", "city": "myslenice",
     "lat": 49.834599, "lon": 19.949989, "match": ("estakada",)},
    {"id": "borek", "label": "Kraków (Borek Fałęcki)", "city": "krakow",
     "lat": 50.011593, "lon": 19.927406, "match": ("borek",)},
    {"id": "matecznego", "label": "Kraków (Rondo Matecznego)", "city": "krakow",
     "lat": 50.036957, "lon": 19.940329, "match": ("matecznego",)},
    {"id": "jubilat", "label": "Kraków (Jubilat)", "city": "krakow",
     "lat": 50.056000, "lon": 19.927246, "match": ("jubilat",)},
    {"id": "agh", "label": "Kraków (AGH)", "city": "krakow",
     "lat": 50.064457, "lon": 19.924274, "match": ("agh",)},
    {"id": "inwalidow", "label": "Kraków (Plac Inwalidów)", "city": "krakow",
     "lat": 50.069244, "lon": 19.926331, "match": ("inwalidow",)},
    {"id": "mda", "label": "Kraków (MDA)", "city": "krakow",
     "lat": 50.067924, "lon": 19.949858, "match": ("mda", "rda", "bosacka")},
]

CACHE_TTL_SECONDS = 6 * 3600
ERROR_RETRY_SECONDS = 60
DETAILS_PAUSE_SECONDS = 0.15
MAX_DAYS_AHEAD = 60
WARM_DAYS = 2                      # today and tomorrow, both directions
WARM_INTERVAL_SECONDS = 30 * 60    # well inside CACHE_TTL_SECONDS
ALERT_GRACE_MINUTES = 3


def match_stop(name: str) -> str | None:
    folded = _fold(name)
    for stop in STOPS:
        if any(fragment in folded for fragment in stop["match"]):
            return stop["id"]
    return None


def stop_label(name: str) -> str:
    """Short display name, e.g. "Kraków, MDA, ul. Bosacka" → "Kraków (MDA)"."""
    stop_id = match_stop(name)
    if stop_id == "mda" and "rda" in _fold(name):
        return "Kraków (RDA)"
    for stop in STOPS:
        if stop["id"] == stop_id:
            return stop["label"]
    return name


def validate_request(direction: str, travel_date: str, *, today: date_type | None = None) -> None:
    if direction not in DIRECTIONS:
        raise BusScheduleError("Nieznany kierunek.")
    try:
        parsed = date_type.fromisoformat(travel_date)
    except (TypeError, ValueError) as exc:
        raise BusScheduleError("Data musi mieć format YYYY-MM-DD.") from exc
    current = today or datetime.now(LOCAL_TIMEZONE).date()
    if not current - timedelta(days=1) <= parsed <= current + timedelta(days=MAX_DAYS_AHEAD):
        raise BusScheduleError("Rozkład jest dostępny od wczoraj do 60 dni naprzód.")


def _get_with_retry(session: requests.Session, url: str, *, attempts: int = 3, **kwargs: Any) -> requests.Response:
    for attempt in range(attempts):
        try:
            response = session.get(url, timeout=REQUEST_TIMEOUT, **kwargs)
            response.raise_for_status()
            return response
        except requests.RequestException:
            if attempt == attempts - 1:
                raise
            time.sleep(1 + attempt)
    raise AssertionError("unreachable")


def fetch_day(
    direction: str,
    travel_date: str,
    *,
    session_factory: Callable[[], requests.Session] = requests.Session,
    pause: float = DETAILS_PAUSE_SECONDS,
) -> dict[str, Any]:
    """Fetch every direct connection of the day with its stops."""
    from_city, to_city = DIRECTIONS[direction]
    session = session_factory()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "Chrome/140.0 Safari/537.36 OdysseusBusSchedule/1.0"
            )
        }
    )
    token = initialize_session(session)
    from_place = resolve_place(session, from_city, token, request_kind="SOURCE")
    to_place = resolve_place(session, to_city, token, request_kind="DESTINATION")
    html = search_connections(
        session,
        from_place,
        to_place,
        travel_date,
        tab_token=token,
        from_city=from_city,
        to_city=to_city,
        debug_path=None,
        carrier_types=BUS_CARRIER_TYPES,
        start_time="00:00",
    )
    html = extend_search_results(session, html, travel_date)

    connections = []
    for row in parse_connection_rows(html, travel_date):
        stops: list[dict[str, Any]] = []
        if row["details_path"]:
            if pause:
                time.sleep(pause)
            try:
                response = _get_with_retry(
                    session,
                    urljoin(BASE_URL, row["details_path"]),
                    headers={"X-Requested-With": "XMLHttpRequest", "Referer": f"{BASE_URL}{SEARCH_PATH}"},
                )
                stops = parse_route_details(response.text)
            except requests.RequestException:
                logger.warning("bus details fetch failed for %s %s", travel_date, row["departure"])
        for stop in stops:
            stop["stop_id"] = match_stop(stop["name"])
        connections.append(
            {
                "id": f'{row["departure"]}-{row["carrier"]}',
                "departure": row["departure"],
                "arrival": row["arrival"],
                "carrier": row["carrier"],
                "from_stop": row["from_stop"],
                "to_stop": row["to_stop"],
                "from_stop_id": match_stop(row["from_stop"]),
                "from_label": stop_label(row["from_stop"]) if row["from_stop"] else from_city,
                "to_label": stop_label(row["to_stop"]) if row["to_stop"] else to_city,
                "departure_at": row["departure_at"].isoformat(),
                "stops": stops,
            }
        )

    return {
        "direction": direction,
        "date": travel_date,
        "from": from_city,
        "to": to_city,
        "fetched_at": datetime.now(LOCAL_TIMEZONE).isoformat(timespec="seconds"),
        "connections": connections,
    }


class DepartureBoard:
    """Stale-while-revalidate cache of fetched days, one background job per day."""

    def __init__(
        self,
        cache_dir: str | os.PathLike[str],
        *,
        fetcher: Callable[[str, str], dict[str, Any]] = fetch_day,
        ttl: float = CACHE_TTL_SECONDS,
        clock: Callable[[], float] = time.time,
    ):
        self.cache_dir = Path(cache_dir)
        self.fetcher = fetcher
        self.ttl = ttl
        self.clock = clock
        self._lock = threading.Lock()
        # One e-podroznik scrape at a time: parallel paging got connections reset.
        self._fetch_lock = threading.Lock()
        self._jobs: dict[tuple[str, str], threading.Thread] = {}
        self._errors: dict[tuple[str, str], tuple[float, str]] = {}
        self._warmer: threading.Thread | None = None

    def _path(self, key: tuple[str, str]) -> Path:
        return self.cache_dir / f"{key[0]}_{key[1]}.json"

    def _read(self, key: tuple[str, str]) -> tuple[dict[str, Any], float] | None:
        path = self._path(key)
        try:
            return json.loads(path.read_text(encoding="utf-8")), path.stat().st_mtime
        except (OSError, ValueError):
            return None

    def _write(self, key: tuple[str, str], schedule: dict[str, Any]) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        path = self._path(key)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(schedule, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
        os.utime(path, (self.clock(), self.clock()))
        self._prune(key[1])

    def _prune(self, travel_date: str) -> None:
        cutoff = (date_type.fromisoformat(travel_date) - timedelta(days=2)).isoformat()
        for path in self.cache_dir.glob("*.json"):
            if path.stem.rsplit("_", 1)[-1] < cutoff:
                path.unlink(missing_ok=True)

    def _run(self, key: tuple[str, str]) -> None:
        try:
            with self._fetch_lock:
                schedule = self.fetcher(*key)
            self._write(key, schedule)
            with self._lock:
                self._errors.pop(key, None)
        except Exception as exc:  # surfaced to the UI, logged for the operator
            logger.warning("bus schedule fetch failed for %s: %s", key, exc)
            message = str(exc) if isinstance(exc, BusScheduleError) else "Nie udało się pobrać rozkładu z e-podroznik.pl."
            with self._lock:
                self._errors[key] = (self.clock(), message)
        finally:
            with self._lock:
                self._jobs.pop(key, None)

    def _start(self, key: tuple[str, str]) -> None:
        # Caller holds self._lock.
        if key in self._jobs:
            return
        job = threading.Thread(target=self._run, args=(key,), name=f"bus-{key[0]}-{key[1]}", daemon=True)
        self._jobs[key] = job
        job.start()

    def get(self, direction: str, travel_date: str, *, refresh: bool = False) -> dict[str, Any]:
        key = (direction, travel_date)
        cached = self._read(key)
        with self._lock:
            loading = key in self._jobs
            error = self._errors.get(key)
            stale = cached is None or refresh or self.clock() - cached[1] > self.ttl
            recent_error = error is not None and self.clock() - error[0] < ERROR_RETRY_SECONDS
            if stale and not loading and not (recent_error and not refresh):
                self._start(key)
                loading = True
        if cached is not None:
            return {"status": "ready", "refreshing": loading, "schedule": cached[0]}
        if loading:
            return {"status": "loading"}
        return {"status": "error", "error": error[1] if error else "Brak danych."}

    def wait(self, timeout: float = 5.0) -> None:
        """Join running jobs (tests)."""
        with self._lock:
            jobs = list(self._jobs.values())
        for job in jobs:
            job.join(timeout)

    def get_ready(self, direction: str, travel_date: str, *, timeout: float = 60.0, poll: float = 1.0) -> dict[str, Any]:
        """Block until the day is cached (or failed / timed out) — for callers without a UI to poll."""
        deadline = time.monotonic() + timeout
        result = self.get(direction, travel_date)
        while result["status"] == "loading" and time.monotonic() < deadline:
            time.sleep(poll)
            result = self.get(direction, travel_date)
        return result

    def warm(self, *, days: int = WARM_DAYS, today: date_type | None = None) -> None:
        """Queue refreshes of stale days so opening the board never waits."""
        current = today or datetime.now(LOCAL_TIMEZONE).date()
        for offset in range(days):
            travel_date = (current + timedelta(days=offset)).isoformat()
            for direction in DIRECTIONS:
                self.get(direction, travel_date)

    def start_warmer(self, *, interval: float = WARM_INTERVAL_SECONDS, initial_delay: float = 60.0) -> None:
        if self._warmer is not None:
            return

        def loop() -> None:
            time.sleep(initial_delay)
            while True:
                try:
                    self.warm()
                except Exception:
                    logger.exception("bus schedule warm-up failed")
                time.sleep(interval)

        self._warmer = threading.Thread(target=loop, name="bus-warmer", daemon=True)
        self._warmer.start()


class LeaveAlerts:
    """"Time to leave" pushes for chosen buses, persisted so restarts keep them."""

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        sender: Callable[[str, str], tuple[bool, str]],
        clock: Callable[[], datetime] = lambda: datetime.now(LOCAL_TIMEZONE),
    ):
        self.path = Path(path)
        self.sender = sender
        self.clock = clock
        self._lock = threading.Lock()
        self._loop: threading.Thread | None = None

    def _load(self) -> list[dict[str, Any]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except (OSError, ValueError):
            return []

    def _save(self, alerts: list[dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(alerts, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)

    def add(self, owner: str, alert: dict[str, Any]) -> dict[str, Any]:
        leave_at = datetime.fromisoformat(alert["leave_at"])
        if leave_at.tzinfo is None:
            leave_at = leave_at.replace(tzinfo=LOCAL_TIMEZONE)
        if leave_at <= self.clock():
            raise BusScheduleError("Na ten kurs trzeba wyjść już teraz.")
        record = {
            "id": f'{alert["date"]}-{alert["connection_id"]}-{alert["stop_id"]}',
            "owner": owner,
            "date": alert["date"],
            "connection_id": alert["connection_id"],
            "stop_id": alert["stop_id"],
            "leave_at": leave_at.isoformat(),
            "title": str(alert.get("title") or "Czas wyjść na busa")[:200],
            "body": str(alert.get("body") or "")[:500],
        }
        with self._lock:
            alerts = [a for a in self._load() if a["id"] != record["id"] or a["owner"] != owner]
            alerts.append(record)
            self._save(alerts)
        return record

    def for_owner(self, owner: str) -> list[dict[str, Any]]:
        with self._lock:
            return [a for a in self._load() if a["owner"] == owner]

    def remove(self, owner: str, alert_id: str) -> bool:
        with self._lock:
            alerts = self._load()
            kept = [a for a in alerts if not (a["owner"] == owner and a["id"] == alert_id)]
            self._save(kept)
            return len(kept) != len(alerts)

    def dispatch_due(self) -> list[dict[str, Any]]:
        """Send and drop every alert whose leave time has come; returns the sent ones."""
        now = self.clock()
        with self._lock:
            alerts = self._load()
            due = [a for a in alerts if datetime.fromisoformat(a["leave_at"]) <= now]
            if not due:
                return []
            self._save([a for a in alerts if a not in due])
        for alert in due:
            # Long overdue (server was down): a late "leave now" would mislead.
            if now - datetime.fromisoformat(alert["leave_at"]) > timedelta(minutes=ALERT_GRACE_MINUTES):
                continue
            sent, error = self.sender(alert["title"], alert["body"])
            if not sent:
                logger.warning("bus leave alert %s not delivered: %s", alert["id"], error)
        return due

    def start(self, *, interval: float = 15.0) -> None:
        if self._loop is not None:
            return

        def loop() -> None:
            while True:
                try:
                    self.dispatch_due()
                except Exception:
                    logger.exception("bus leave alerts loop failed")
                time.sleep(interval)

        self._loop = threading.Thread(target=loop, name="bus-leave-alerts", daemon=True)
        self._loop.start()
