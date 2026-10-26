"""Minimal e-podroznik client used by the local bus-schedule MCP tool."""

from __future__ import annotations

import re
import unicodedata
from datetime import date as date_type
from datetime import datetime, time
from pathlib import Path
from typing import Any
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup


BASE_URL = "https://www.e-podroznik.pl"
SEARCH_PATH = "/public/searchingResults.do?method=task"
DEFAULT_DEBUG_PATH = Path("/app/data/mcp_tools/debug_search_result.html")
REQUEST_TIMEOUT = 35
MAX_RESULT_EXTENSIONS = 24
ALL_CARRIER_TYPES = ("1", "2", "3", "4", "5")
# e-podroznik's "bus" carrier type: the only one Maxbus, Majerbus and Szwagropol
# are listed under. Searching it alone avoids paging through trains and RAB BUS.
BUS_CARRIER_TYPES = ("1",)
LOCAL_TIMEZONE = ZoneInfo("Europe/Warsaw")

_CITY_TYPES = {"CITY", "BIG_CITY", "VERY_BIG_CITY"}
_TOKEN_PATTERNS = (
    re.compile(r'name=["\']tabToken["\']\s*value=["\']([0-9a-f]{32})', re.I),
    re.compile(r'\btabToken\b\s*[:=]\s*["\']([0-9a-f]{32})["\']', re.I),
    re.compile(r'EPodroznik\.setTabToken\(["\']([0-9a-f]{32})["\']\)', re.I),
)


class BusScheduleError(RuntimeError):
    """A user-facing failure while querying or parsing e-podroznik."""


def _fold(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value or "")
    ascii_value = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]", "", ascii_value.casefold())


def _exact_name(value: str) -> str:
    return unicodedata.normalize("NFC", value or "").strip().casefold()


def normalize_carrier(name: str) -> str | None:
    """Map source name variants to the three carriers supported by the MVP."""
    folded = _fold(name)
    if "maxbus" in folded:
        return "Maxbus"
    if "majerbus" in folded:
        return "Majerbus"
    if "szwagropol" in folded:
        return "Szwagropol"
    return None


def _extract_tab_token(html: str) -> str:
    for pattern in _TOKEN_PATTERNS:
        match = pattern.search(html)
        if match:
            return match.group(1)
    raise BusScheduleError("Nie udało się pobrać tabToken z e-podroznik.pl.")


def initialize_session(session: requests.Session) -> str:
    response = session.get(f"{BASE_URL}/", timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    return _extract_tab_token(response.text)


def resolve_place(
    session: requests.Session,
    city_name: str,
    tab_token: str | None = None,
    *,
    request_kind: str = "SOURCE",
) -> str:
    """Resolve a city name to e-podroznik's dynamic placeDataString."""
    query = (city_name or "").strip()
    if not query:
        raise BusScheduleError("Nazwa miejscowości nie może być pusta.")
    if request_kind not in {"SOURCE", "DESTINATION"}:
        raise ValueError("request_kind must be SOURCE or DESTINATION")

    token = tab_token or initialize_session(session)
    response = session.post(
        f"{BASE_URL}/public/suggest.do",
        data={
            "query": query,
            "type": "CITIES",
            "requestKind": request_kind,
            "countryCode": "",
            "forcingCountryCode": "false",
            "tabToken": token,
        },
        headers={
            "X-Requested-With": "XMLHttpRequest",
            "Origin": BASE_URL,
            "Referer": f"{BASE_URL}/",
        },
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    try:
        payload = response.json()
    except ValueError as exc:
        raise BusScheduleError("e-podroznik.pl zwrócił nieprawidłową listę miejsc.") from exc

    suggestions = payload.get("suggestions", []) if isinstance(payload, dict) else []
    candidates = [
        item
        for item in suggestions
        if isinstance(item, dict)
        and item.get("t") in _CITY_TYPES
        and item.get("placeDataString")
    ]
    exact = [item for item in candidates if _exact_name(str(item.get("n", ""))) == _exact_name(query)]
    accent_insensitive = [item for item in candidates if _fold(str(item.get("n", ""))) == _fold(query)]
    chosen = exact[0] if exact else (accent_insensitive[0] if accent_insensitive else None)
    if not chosen:
        raise BusScheduleError(f"Nie znaleziono miejscowości: {query}.")
    return str(chosen["placeDataString"])


def search_connections(
    session: requests.Session,
    from_place: str,
    to_place: str,
    travel_date: str,
    *,
    tab_token: str,
    from_city: str,
    to_city: str,
    debug_path: Path | None = DEFAULT_DEBUG_PATH,
    carrier_types: tuple[str, ...] = ALL_CARRIER_TYPES,
    start_time: str | None = None,
) -> str:
    """Submit a one-way search using the initialized session and tabToken.

    Without start_time e-podroznik starts today's results at the current time;
    pass "00:00" to get the whole day.
    """
    try:
        parsed_date = date_type.fromisoformat(travel_date)
    except (TypeError, ValueError) as exc:
        raise BusScheduleError("Data musi mieć format YYYY-MM-DD.") from exc

    prefix = "formCompositeSearchingResults.formCompositeSearcherFinalH."
    data: list[tuple[str, str]] = [
        ("tseVw", "regularP"),
        ("tabToken", tab_token),
        ("fromV", from_place),
        ("toV", to_place),
        ("tripType", "one-way"),
        (f"{prefix}fromText", from_city),
        (f"{prefix}toText", to_city),
        (f"{prefix}dateV", parsed_date.strftime("%d.%m.%Y")),
        (f"{prefix}arrivalV", "DEPARTURE"),
        (f"{prefix}timeV", start_time or ""),
    ]
    if start_time is None:
        data.append((f"{prefix}ommitTime", "on"))
    data.append(("minimalTimeForChangeV", ""))
    data.extend((f"{prefix}carrierTypes", value) for value in carrier_types)

    response = session.post(
        f"{BASE_URL}{SEARCH_PATH}",
        data=data,
        headers={"Origin": BASE_URL, "Referer": f"{BASE_URL}/"},
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    if not response.text.strip():
        raise BusScheduleError("e-podroznik.pl zwrócił pustą odpowiedź wyszukiwania.")

    if debug_path is not None:
        debug_path.parent.mkdir(parents=True, exist_ok=True)
        debug_path.write_text(response.text, encoding="utf-8")
    return response.text


def _row_departure_datetime(row: Any, fallback_date: date_type | None = None) -> datetime | None:
    raw_timestamp = row.get("data-departure-timestamp")
    if raw_timestamp:
        try:
            return datetime.fromtimestamp(int(raw_timestamp) / 1000, LOCAL_TIMEZONE)
        except (TypeError, ValueError, OSError):
            pass

    departure = _text(row.select_one(".edge-date-time .departure .time"))
    if fallback_date is None or not re.fullmatch(r"\d{2}:\d{2}", departure):
        return None
    parsed_time = time.fromisoformat(departure)
    return datetime.combine(fallback_date, parsed_time, LOCAL_TIMEZONE)


def extend_search_results(
    session: requests.Session,
    initial_html: str,
    travel_date: str,
    *,
    max_extensions: int = MAX_RESULT_EXTENSIONS,
) -> str:
    """Follow the cumulative "later connections" result until the day is complete."""
    try:
        target_date = date_type.fromisoformat(travel_date)
    except (TypeError, ValueError) as exc:
        raise BusScheduleError("Data musi mieć format YYYY-MM-DD.") from exc

    html = initial_html
    seen_pages: set[tuple[tuple[str, str], ...]] = set()

    for extension_number in range(max_extensions + 1):
        soup = BeautifulSoup(html, "html.parser")
        rows = soup.select("div.searching-result")
        fingerprint = tuple(
            (
                str(row.get("data-departure-timestamp", "")),
                str(row.get("data-connection-search-id", row.get("id", ""))),
            )
            for row in rows
        )
        if fingerprint in seen_pages:
            raise BusScheduleError("e-podroznik.pl nie zwrócił kolejnego okna wyników.")
        seen_pages.add(fingerprint)

        departures = [_row_departure_datetime(row) for row in rows]
        if any(value and value.date() > target_date for value in departures):
            return html

        later_link = soup.select_one("a.btnSearchForLater[href]")
        if later_link is None:
            return html
        if extension_number >= max_extensions:
            raise BusScheduleError("Nie udało się pobrać całego dnia wyników z e-podroznik.pl.")

        try:
            response = session.get(
                urljoin(BASE_URL, str(later_link["href"])),
                headers={"Referer": f"{BASE_URL}{SEARCH_PATH}"},
                timeout=REQUEST_TIMEOUT,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            raise BusScheduleError(
                "Nie udało się pobrać pełnej listy późniejszych połączeń z e-podroznik.pl."
            ) from exc
        if not response.text.strip():
            raise BusScheduleError("e-podroznik.pl zwrócił puste okno późniejszych połączeń.")
        html = response.text

    raise BusScheduleError("Nie udało się pobrać całego dnia wyników z e-podroznik.pl.")


def _text(node: Any) -> str:
    return node.get_text(" ", strip=True) if node else ""


def _row_carrier(row: Any) -> str | None:
    """Return the single supported carrier of a direct connection row."""
    # A row with several legs is a transfer (e.g. FlixBus to Nowy Targ, then
    # Maxbus); its carrier nodes may list only the supported leg.
    if str(row.get("data-connectionscount") or "1") != "1":
        return None

    raw_names: list[str] = []
    for carrier_node in row.select("[data-carrier-name], .carrier-name"):
        raw_name = (carrier_node.get("data-carrier-name") or _text(carrier_node)).strip()
        raw_name = re.sub(r"\s*\(przedsiębiorca\)\s*$", "", raw_name, flags=re.I)
        if raw_name and raw_name not in raw_names:
            raw_names.append(raw_name)

    normalized = {normalize_carrier(raw_name) for raw_name in raw_names}
    normalized.discard(None)
    unknown = any(normalize_carrier(raw_name) is None for raw_name in raw_names)
    if unknown or len(normalized) != 1:
        return None
    return normalized.pop()


def parse_connection_rows(html: str, travel_date: str) -> list[dict[str, Any]]:
    """Extract every direct target-carrier connection departing on travel_date.

    Unlike parse_connections this keeps departed connections and the
    session-bound details path, which the departures board needs.
    """
    try:
        target_date = date_type.fromisoformat(travel_date)
    except (TypeError, ValueError) as exc:
        raise BusScheduleError("Data musi mieć format YYYY-MM-DD.") from exc

    soup = BeautifulSoup(html, "html.parser")
    connections: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()

    for row in soup.select("div.searching-result"):
        departure_datetime = _row_departure_datetime(row, target_date)
        if departure_datetime is None or departure_datetime.date() != target_date:
            continue

        departure = _text(row.select_one(".edge-date-time .departure .time"))
        arrival = _text(row.select_one(".edge-date-time .arrival .time"))
        if not re.fullmatch(r"\d{2}:\d{2}", departure) or not re.fullmatch(r"\d{2}:\d{2}", arrival):
            continue

        carrier = _row_carrier(row)
        if carrier is None:
            continue

        key = (departure, arrival, carrier)
        if key in seen:
            continue
        seen.add(key)
        connections.append(
            {
                "departure": departure,
                "arrival": arrival,
                "carrier": carrier,
                "from_stop": _text(row.select_one(".edge-stops-names .departure")),
                "to_stop": _text(row.select_one(".edge-stops-names .arrival")),
                "departure_at": departure_datetime,
                "details_path": str(row.get("data-detailspath") or ""),
            }
        )

    return sorted(connections, key=lambda item: item["departure_at"])


def parse_connections(
    html: str,
    travel_date: str | None = None,
    *,
    now: datetime | None = None,
) -> list[dict[str, str]]:
    """Extract direct target-carrier connections from a results page."""
    target_date: date_type | None = None
    if travel_date is not None:
        try:
            target_date = date_type.fromisoformat(travel_date)
        except (TypeError, ValueError) as exc:
            raise BusScheduleError("Data musi mieć format YYYY-MM-DD.") from exc

    current_time = now or datetime.now(LOCAL_TIMEZONE)
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=LOCAL_TIMEZONE)
    else:
        current_time = current_time.astimezone(LOCAL_TIMEZONE)

    soup = BeautifulSoup(html, "html.parser")
    connections: list[dict[str, str]] = []

    for row in soup.select("div.searching-result"):
        departure_datetime = _row_departure_datetime(row, target_date)
        if target_date is not None:
            if departure_datetime is None or departure_datetime.date() != target_date:
                continue
            if target_date <= current_time.date() and departure_datetime < current_time:
                continue

        departure = _text(row.select_one(".edge-date-time .departure .time"))
        arrival = _text(row.select_one(".edge-date-time .arrival .time"))
        if not re.fullmatch(r"\d{2}:\d{2}", departure) or not re.fullmatch(r"\d{2}:\d{2}", arrival):
            continue

        carrier = _row_carrier(row)
        if carrier is None:
            continue

        connection = {"departure": departure, "arrival": arrival, "carrier": carrier}
        # Boarding and alighting stops, e.g. "Kraków, MDA, ul. Bosacka".
        from_stop = _text(row.select_one(".edge-stops-names .departure"))
        to_stop = _text(row.select_one(".edge-stops-names .arrival"))
        if from_stop:
            connection["from_stop"] = from_stop
        if to_stop:
            connection["to_stop"] = to_stop
        if connection not in connections:
            connections.append(connection)

    return sorted(connections, key=lambda item: item["departure"])


def _stop_time(value: str) -> str:
    match = re.search(r"\d{2}:\d{2}", value or "")
    return match.group(0) if match else ""


def parse_route_details(html: str) -> list[dict[str, str]]:
    """Extract the ordered stops from a connection's "Szczegóły trasy" page."""
    soup = BeautifulSoup(html, "html.parser")
    stops: list[dict[str, str]] = []
    for details in soup.select(".route-details"):
        name = _text(details.select_one(".stop-name"))
        if not name:
            continue
        arrival = _stop_time(_text(details.select_one(".arrival")))
        departure = _stop_time(_text(details.select_one(".departure")))
        container = details.find_parent(class_=["stop-item", "route-details-container"])
        day = container.select_one(".route-day[data-associatedday]") if container else None
        raw_day = str(day.get("data-associatedday", "")) if day else ""
        stop_date = ""
        if re.fullmatch(r"\d{8}", raw_day):
            stop_date = f"{raw_day[4:]}-{raw_day[2:4]}-{raw_day[:2]}"
        stops.append(
            {
                "name": name,
                "arrival": arrival or departure,
                "departure": departure or arrival,
                "date": stop_date,
            }
        )
    return stops


def get_bus_schedule(from_city: str, to_city: str, travel_date: str) -> dict[str, Any]:
    """Return the final JSON-ready result for the MCP tool."""
    # Validate before performing any network I/O.
    try:
        date_type.fromisoformat(travel_date)
    except (TypeError, ValueError) as exc:
        raise BusScheduleError("Data musi mieć format YYYY-MM-DD.") from exc

    session = requests.Session()
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
    )
    html = extend_search_results(session, html, travel_date)
    return {
        "from": from_city,
        "to": to_city,
        "date": travel_date,
        "connections": parse_connections(html, travel_date),
    }
