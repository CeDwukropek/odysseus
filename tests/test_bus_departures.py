import json
import os
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routes.bus_routes import setup_bus_routes
from src import bus_departures
from src.bus_departures import DepartureBoard, fetch_day, match_stop, stop_label, validate_request
from src.tools.bus_schedule import BusScheduleError

FIXTURES = Path(__file__).parent / "fixtures" / "bus_schedule"
WARSAW = ZoneInfo("Europe/Warsaw")


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Rabka Zdrój, Zabornia", "zabornia"),
        ("Rabka-Zdrój, RABKA - ZABORNIA", "zabornia"),
        ('Skomielna Biała "skrzyżowanie"', "skomielna"),
        ("Kraków AGH d/w", "agh"),
        ("Kraków AGH/UR", "agh"),
        ("Kraków Pl. Inwalidów", "inwalidow"),
        ("Kraków, RDA", "mda"),
        ("Kraków, MDA, ul. Bosacka", "mda"),
        ("KRAKÓW MDA", "mda"),
        ("Nowy Targ, D.A.", None),
    ],
)
def test_match_stop_folds_carrier_spellings(name, expected):
    assert match_stop(name) == expected


def test_stop_label_keeps_rda_distinct_from_mda():
    assert stop_label("Kraków, MDA, ul. Bosacka") == "Kraków (MDA)"
    assert stop_label("Kraków, RDA") == "Kraków (RDA)"
    assert stop_label("Rabka-Zdrój, RABKA - ZABORNIA") == "Rabka (Zabornia)"
    assert stop_label("Nowy Targ, D.A.") == "Nowy Targ, D.A."


def test_validate_request_bounds_direction_and_date():
    today = date(2026, 10, 26)
    validate_request("to_krakow", "2026-10-25", today=today)
    validate_request("to_rabka", "2026-12-25", today=today)
    with pytest.raises(BusScheduleError, match="kierunek"):
        validate_request("to_zakopane", "2026-10-26", today=today)
    with pytest.raises(BusScheduleError, match="YYYY-MM-DD"):
        validate_request("to_krakow", "26.10.2026", today=today)
    with pytest.raises(BusScheduleError, match="60 dni"):
        validate_request("to_krakow", "2026-10-24", today=today)


class Clock:
    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


def schedule(travel_date="2026-10-27", marker="first"):
    return {"direction": "to_krakow", "date": travel_date, "connections": [], "marker": marker}


def test_board_loads_in_background_then_serves_cache(tmp_path):
    calls = []

    def fetcher(direction, travel_date):
        calls.append((direction, travel_date))
        return schedule(travel_date)

    board = DepartureBoard(tmp_path, fetcher=fetcher, clock=Clock())

    first = board.get("to_krakow", "2026-10-27")
    board.wait()
    second = board.get("to_krakow", "2026-10-27")

    assert first["status"] in {"loading", "ready"}
    assert second == {"status": "ready", "refreshing": False, "schedule": schedule()}
    assert calls == [("to_krakow", "2026-10-27")]
    assert json.loads((tmp_path / "to_krakow_2026-10-27.json").read_text())["marker"] == "first"


def test_board_serves_stale_copy_while_refreshing(tmp_path):
    clock = Clock()
    results = iter([schedule(marker="first"), schedule(marker="second")])
    board = DepartureBoard(tmp_path, fetcher=lambda *_: next(results), clock=clock, ttl=60)

    board.get("to_krakow", "2026-10-27")
    board.wait()
    clock.now += 61
    stale = board.get("to_krakow", "2026-10-27")
    board.wait()

    assert stale["status"] == "ready"
    assert stale["schedule"]["marker"] == "first"
    assert board.get("to_krakow", "2026-10-27")["schedule"]["marker"] == "second"


def test_board_reports_errors_and_waits_before_retrying(tmp_path):
    clock = Clock()
    calls = []

    def fetcher(*key):
        calls.append(key)
        raise BusScheduleError("Nie znaleziono miejscowości: Kraków.")

    board = DepartureBoard(tmp_path, fetcher=fetcher, clock=clock)
    board.get("to_rabka", "2026-10-27")
    board.wait()

    assert board.get("to_rabka", "2026-10-27") == {
        "status": "error",
        "error": "Nie znaleziono miejscowości: Kraków.",
    }
    assert len(calls) == 1

    clock.now += bus_departures.ERROR_RETRY_SECONDS + 1
    board.get("to_rabka", "2026-10-27")
    board.wait()
    assert len(calls) == 2


def test_board_hides_unexpected_error_details(tmp_path):
    def fetcher(*_):
        raise RuntimeError("socket internals")

    board = DepartureBoard(tmp_path, fetcher=fetcher, clock=Clock())
    board.get("to_rabka", "2026-10-27")
    board.wait()

    assert board.get("to_rabka", "2026-10-27")["error"] == "Nie udało się pobrać rozkładu z e-podroznik.pl."


def test_board_prunes_days_older_than_two_days(tmp_path):
    (tmp_path / "to_krakow_2026-10-20.json").write_text("{}")
    (tmp_path / "to_rabka_2026-10-25.json").write_text("{}")
    board = DepartureBoard(tmp_path, fetcher=lambda d, t: schedule(t), clock=Clock())

    board.get("to_krakow", "2026-10-27")
    board.wait()

    assert sorted(os.listdir(tmp_path)) == ["to_krakow_2026-10-27.json", "to_rabka_2026-10-25.json"]


class FakeResponse:
    def __init__(self, text="", payload=None):
        self.text = text
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class FakeEpodroznik:
    """Serves the e-podroznik flow: home page, suggestions, search, details."""

    def __init__(self, results_html, details_html):
        self.headers = {}
        self.results_html = results_html
        self.details_html = details_html
        self.searches = []
        self.details = []

    def get(self, url, **kwargs):
        if "searchingResultExtended" in url:
            self.details.append(url)
            return FakeResponse(self.details_html)
        return FakeResponse('<input name="tabToken" value="' + "a" * 32 + '">')

    def post(self, url, **kwargs):
        if url.endswith("/public/suggest.do"):
            query = kwargs["data"]["query"]
            return FakeResponse(payload={"suggestions": [{"n": query, "t": "CITY", "placeDataString": f"c|{query}"}]})
        self.searches.append(kwargs["data"])
        return FakeResponse(self.results_html)


def test_fetch_day_attaches_stops_and_labels():
    departure = datetime(2026, 10, 27, 6, 10, tzinfo=WARSAW)
    row = f"""
    <div class="searching-result" data-connectionscount="1"
         data-departure-timestamp="{int(departure.timestamp() * 1000)}"
         data-detailspath="/public/searchingResultExtended.do?resultId=1">
      <span class="edge-stops-names"><span class="departure">KRAKÓW MDA</span>
      <span class="arrival">Rabka-Zdrój, RABKA - ZABORNIA</span></span>
      <span class="edge-date-time"><span class="departure"><span class="time">06:10</span></span>
      <span class="arrival"><span class="time">07:21</span></span></span>
      <span class="carrier-name">Szwagropol</span>
    </div>
    """
    session = FakeEpodroznik(row, (FIXTURES / "route_details_krakow_rabka.html").read_text(encoding="utf-8"))

    result = fetch_day("to_rabka", "2026-10-27", session_factory=lambda: session, pause=0)

    sent = dict(session.searches[0])
    assert [value for key, value in session.searches[0] if key.endswith("carrierTypes")] == ["1"]
    # Whole day even when fetching today, not just from the current time.
    assert sent["formCompositeSearchingResults.formCompositeSearcherFinalH.timeV"] == "00:00"
    assert "formCompositeSearchingResults.formCompositeSearcherFinalH.ommitTime" not in sent
    assert session.details == ["https://www.e-podroznik.pl/public/searchingResultExtended.do?resultId=1"]
    connection = result["connections"][0]
    assert connection["from_stop_id"] == "mda"
    assert connection["from_label"] == "Kraków (MDA)"
    assert connection["to_label"] == "Rabka (Zabornia)"
    assert connection["departure_at"] == "2026-10-27T06:10:00+01:00"
    assert [stop["stop_id"] for stop in connection["stops"]] == [
        "mda", "agh", "jubilat", "matecznego", "borek", "myslenice", "skomielna", "zabornia",
    ]


def test_schedule_route_validates_and_delegates(tmp_path):
    board = DepartureBoard(tmp_path, fetcher=lambda d, t: schedule(t), clock=Clock())
    app = FastAPI()
    app.include_router(setup_bus_routes(board))
    client = TestClient(app)

    assert client.get("/api/buses/schedule", params={"direction": "north"}).status_code == 400
    client.get("/api/buses/schedule", params={"direction": "to_krakow", "date": "2026-10-27"})
    board.wait()
    response = client.get("/api/buses/schedule", params={"direction": "to_krakow", "date": "2026-10-27"})
    stops = client.get("/api/buses/stops").json()["stops"]

    assert response.json()["status"] == "ready"
    assert stops[0]["id"] == "zabornia"
    assert "match" not in stops[0]
