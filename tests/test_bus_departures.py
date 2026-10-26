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
from src.bus_departures import (
    DepartureBoard,
    LeaveAlerts,
    fetch_day,
    match_stop,
    stop_label,
    validate_request,
)
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


def _client(tmp_path, board, *, push_ready=True):
    alerts = LeaveAlerts(
        tmp_path / "alerts.json",
        sender=lambda title, body: (True, ""),
        clock=lambda: datetime(2026, 10, 27, 7, 0, tzinfo=WARSAW),
    )
    app = FastAPI()
    app.include_router(setup_bus_routes(board, alerts, notifier_ready=lambda: push_ready))
    return TestClient(app), alerts


def test_schedule_route_validates_and_delegates(tmp_path):
    board = DepartureBoard(tmp_path, fetcher=lambda d, t: schedule(t), clock=Clock())
    client, _ = _client(tmp_path, board)

    assert client.get("/api/buses/schedule", params={"direction": "north"}).status_code == 400
    client.get("/api/buses/schedule", params={"direction": "to_krakow", "date": "2026-10-27"})
    board.wait()
    response = client.get("/api/buses/schedule", params={"direction": "to_krakow", "date": "2026-10-27"})
    stops = client.get("/api/buses/stops").json()["stops"]

    assert response.json()["status"] == "ready"
    assert stops[0]["id"] == "zabornia"
    assert "match" not in stops[0]


def test_board_warm_queues_today_and_tomorrow_both_ways_one_fetch_at_a_time(tmp_path):
    calls = []
    running = []

    def fetcher(direction, travel_date):
        running.append(1)
        assert len(running) == 1, "e-podroznik must be scraped one day at a time"
        calls.append((direction, travel_date))
        running.pop()
        return schedule(travel_date)

    board = DepartureBoard(tmp_path, fetcher=fetcher, clock=Clock())
    board.warm(today=date(2026, 10, 27))
    board.wait()
    board.warm(today=date(2026, 10, 27))  # fresh now: nothing to refetch
    board.wait()

    assert sorted(calls) == [
        ("to_krakow", "2026-10-27"), ("to_krakow", "2026-10-28"),
        ("to_rabka", "2026-10-27"), ("to_rabka", "2026-10-28"),
    ]


def test_board_get_ready_waits_for_the_background_fetch(tmp_path):
    board = DepartureBoard(tmp_path, fetcher=lambda d, t: schedule(t), clock=Clock())

    result = board.get_ready("to_krakow", "2026-10-27", timeout=5, poll=0.01)

    assert result["status"] == "ready"


def test_leave_alerts_fire_once_when_due(tmp_path):
    now = [datetime(2026, 10, 27, 7, 0, tzinfo=WARSAW)]
    sent = []
    alerts = LeaveAlerts(tmp_path / "alerts.json", sender=lambda t, b: (sent.append((t, b)), (True, ""))[1], clock=lambda: now[0])
    record = alerts.add("emil", {
        "date": "2026-10-27", "connection_id": "07:38-Majerbus", "stop_id": "zabornia",
        "leave_at": "2026-10-27T07:20:00+01:00", "title": "Wyjdź na busa 07:38", "body": "Majerbus z Zabornii",
    })

    assert alerts.dispatch_due() == []
    now[0] = datetime(2026, 10, 27, 7, 20, 30, tzinfo=WARSAW)
    assert [a["id"] for a in alerts.dispatch_due()] == [record["id"]]
    assert alerts.dispatch_due() == []
    assert sent == [("Wyjdź na busa 07:38", "Majerbus z Zabornii")]
    assert alerts.for_owner("emil") == []


def test_leave_alerts_skip_long_overdue_and_reject_past(tmp_path):
    now = [datetime(2026, 10, 27, 7, 0, tzinfo=WARSAW)]
    sent = []
    alerts = LeaveAlerts(tmp_path / "alerts.json", sender=lambda t, b: (sent.append(t), (True, ""))[1], clock=lambda: now[0])
    alerts.add("emil", {"date": "2026-10-27", "connection_id": "a", "stop_id": "agh", "leave_at": "2026-10-27T07:10:00+01:00"})
    with pytest.raises(BusScheduleError):
        alerts.add("emil", {"date": "2026-10-27", "connection_id": "b", "stop_id": "agh", "leave_at": "2026-10-27T06:59:00+01:00"})

    now[0] = datetime(2026, 10, 27, 7, 30, tzinfo=WARSAW)  # server was down past the grace window
    assert len(alerts.dispatch_due()) == 1
    assert sent == []


def test_leave_alerts_are_per_owner(tmp_path):
    alerts = LeaveAlerts(tmp_path / "alerts.json", sender=lambda t, b: (True, ""),
                         clock=lambda: datetime(2026, 10, 27, 7, 0, tzinfo=WARSAW))
    record = alerts.add("emil", {"date": "2026-10-27", "connection_id": "a", "stop_id": "agh", "leave_at": "2026-10-27T07:10:00+01:00"})

    assert alerts.for_owner("other") == []
    assert alerts.remove("other", record["id"]) is False
    assert alerts.remove("emil", record["id"]) is True


def test_alert_routes_round_trip(tmp_path):
    board = DepartureBoard(tmp_path, fetcher=lambda d, t: schedule(t), clock=Clock())
    client, _ = _client(tmp_path, board, push_ready=False)
    payload = {"date": "2026-10-27", "connection_id": "07:38-Majerbus", "stop_id": "zabornia",
               "leave_at": "2026-10-27T06:20:00.000Z", "title": "Wyjdź", "body": "teraz"}

    created = client.post("/api/buses/alerts", json=payload).json()
    listed = client.get("/api/buses/alerts").json()
    past = client.post("/api/buses/alerts", json={**payload, "leave_at": "2026-10-27T05:00:00Z"})
    deleted = client.delete(f'/api/buses/alerts/{created["alert"]["id"]}')
    missing = client.delete(f'/api/buses/alerts/{created["alert"]["id"]}')

    assert created["push_ready"] is False
    assert created["alert"]["leave_at"].startswith("2026-10-27T06:20:00")
    assert [a["id"] for a in listed["alerts"]] == [created["alert"]["id"]]
    assert past.status_code == 400
    assert deleted.status_code == 200
    assert missing.status_code == 404


def test_push_ntfy_uses_integration_topic_and_utf8_title(monkeypatch):
    from src import ntfy_push

    calls = []

    class Response:
        ok = True
        status_code = 200

    monkeypatch.setattr("src.integrations.load_integrations",
                        lambda: [{"preset": "ntfy", "enabled": True, "base_url": "https://ntfy.example/"}])
    monkeypatch.setattr("src.settings.load_settings", lambda: {"reminder_ntfy_topic": "busy"})
    monkeypatch.setattr("src.url_safety.check_outbound_url", lambda url, block_private: (True, ""))
    monkeypatch.setattr(ntfy_push.requests, "post", lambda url, **kw: (calls.append((url, kw)), Response())[1])

    assert ntfy_push.push_ntfy("Wyjdź na busa 07:38", "Majerbus z Zabornii") == (True, "")
    url, kwargs = calls[0]
    assert url == "https://ntfy.example/busy"
    assert kwargs["params"]["title"] == "Wyjdź na busa 07:38"
    assert kwargs["data"] == "Majerbus z Zabornii".encode("utf-8")


def test_push_ntfy_reports_missing_integration(monkeypatch):
    from src import ntfy_push

    monkeypatch.setattr("src.integrations.load_integrations", lambda: [])
    assert ntfy_push.push_ntfy("t", "b") == (False, "No enabled ntfy integration")
    assert ntfy_push.ntfy_configured() is False
