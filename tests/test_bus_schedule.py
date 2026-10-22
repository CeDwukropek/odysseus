from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from src.tools.bus_schedule import (
    BusScheduleError,
    extend_search_results,
    normalize_carrier,
    parse_connections,
    resolve_place,
    search_connections,
)


class FakeResponse:
    def __init__(self, *, text="", payload=None):
        self.text = text
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


class FakePagingSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


def result_row(timestamp_ms, departure, arrival, carrier="Majer Bus"):
    return f"""
    <div class="searching-result" data-departure-timestamp="{timestamp_ms}">
      <span class="edge-date-time"><span class="departure"><span class="time">{departure}</span></span>
      <span class="arrival"><span class="time">{arrival}</span></span></span>
      <span class="carrier-name">{carrier}</span>
    </div>
    """


def timestamp_ms(value):
    return int(value.timestamp() * 1000)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("MAXBUS", "Maxbus"),
        ("Max Bus Zakopane", "Maxbus"),
        ("Majer Bus", "Majerbus"),
        ("Szwagropol (przedsiębiorca)", "Szwagropol"),
        ("PKP Intercity", None),
    ],
)
def test_normalize_carrier(raw, expected):
    assert normalize_carrier(raw) == expected


def test_resolve_place_prefers_exact_city_name():
    response = FakeResponse(
        payload={
            "suggestions": [
                {"n": "Krąków", "t": "CITY", "placeDataString": "c|28002"},
                {"n": "Kraków", "t": "VERY_BIG_CITY", "placeDataString": "c|55156"},
            ]
        }
    )
    session = FakeSession(response)

    assert resolve_place(session, "Kraków", "a" * 32) == "c|55156"


def test_search_connections_builds_expected_payload_and_debug_file(tmp_path: Path):
    response = FakeResponse(text="<html>result</html>")
    session = FakeSession(response)
    debug_path = tmp_path / "debug.html"

    html = search_connections(
        session,
        "c|7201",
        "c|55156",
        "2026-10-05",
        tab_token="a" * 32,
        from_city="Rabka-Zdrój",
        to_city="Kraków",
        debug_path=debug_path,
    )

    assert html == "<html>result</html>"
    assert debug_path.read_text() == html
    sent = dict(session.calls[0][1]["data"])
    assert sent["fromV"] == "c|7201"
    assert sent["toV"] == "c|55156"
    assert sent["formCompositeSearchingResults.formCompositeSearcherFinalH.dateV"] == "05.10.2026"


def test_search_connections_rejects_invalid_date_before_network_call():
    session = FakeSession(FakeResponse(text="unused"))
    with pytest.raises(BusScheduleError, match="YYYY-MM-DD"):
        search_connections(
            session,
            "c|1",
            "c|2",
            "05.10.2026",
            tab_token="a" * 32,
            from_city="A",
            to_city="B",
        )
    assert session.calls == []


def test_parse_filters_unknown_carriers_and_sorts():
    html = """
    <div class="searching-result">
      <span class="edge-date-time"><span class="departure"><span class="time">17:43</span></span>
      <span class="arrival"><span class="time">19:00</span></span></span>
      <span class="carrier-name">Majer Bus</span>
    </div>
    <div class="searching-result">
      <span class="edge-date-time"><span class="departure"><span class="time">16:23</span></span>
      <span class="arrival"><span class="time">17:40</span></span></span>
      <div data-carrier-name="Max Bus Zakopane"></div>
    </div>
    <div class="searching-result">
      <span class="edge-date-time"><span class="departure"><span class="time">16:00</span></span>
      <span class="arrival"><span class="time">17:00</span></span></span>
      <span class="carrier-name">PKP Intercity</span>
    </div>
    """

    assert parse_connections(html) == [
        {"departure": "16:23", "arrival": "17:40", "carrier": "Maxbus"},
        {"departure": "17:43", "arrival": "19:00", "carrier": "Majerbus"},
    ]


def test_extend_search_results_fetches_until_results_reach_next_day():
    warsaw = ZoneInfo("Europe/Warsaw")
    first = result_row(timestamp_ms(datetime(2026, 10, 6, 5, 23, tzinfo=warsaw)), "05:23", "06:40")
    second = result_row(timestamp_ms(datetime(2026, 10, 6, 7, 38, tzinfo=warsaw)), "07:38", "08:55")
    next_day = result_row(timestamp_ms(datetime(2026, 10, 7, 4, 10, tzinfo=warsaw)), "04:10", "05:20")
    later = '<a class="btnSearchForLater" href="/public/searchingResults.do?next=1">later</a>'
    session = FakePagingSession(
        [
            FakeResponse(text=first + second + later),
            FakeResponse(text=first + second + next_day + later),
        ]
    )

    html = extend_search_results(session, first + later, "2026-10-06")

    assert next_day in html
    assert len(session.calls) == 2


def test_parse_filters_adjacent_dates_and_departed_connections_for_today():
    warsaw = ZoneInfo("Europe/Warsaw")
    previous_day = result_row(
        timestamp_ms(datetime(2026, 10, 5, 23, 50, tzinfo=warsaw)), "23:50", "01:00"
    )
    departed = result_row(
        timestamp_ms(datetime(2026, 10, 6, 9, 0, tzinfo=warsaw)), "09:00", "10:10"
    )
    upcoming = result_row(
        timestamp_ms(datetime(2026, 10, 6, 11, 0, tzinfo=warsaw)), "11:00", "12:10"
    )
    next_day = result_row(
        timestamp_ms(datetime(2026, 10, 7, 4, 10, tzinfo=warsaw)), "04:10", "05:20"
    )

    assert parse_connections(
        previous_day + departed + upcoming + next_day,
        "2026-10-06",
        now=datetime(2026, 10, 6, 10, 0, tzinfo=warsaw),
    ) == [{"departure": "11:00", "arrival": "12:10", "carrier": "Majerbus"}]


def test_parse_keeps_all_connections_for_a_future_date():
    warsaw = ZoneInfo("Europe/Warsaw")
    early = result_row(
        timestamp_ms(datetime(2026, 10, 7, 5, 23, tzinfo=warsaw)), "05:23", "06:40", "Max Bus"
    )
    late = result_row(
        timestamp_ms(datetime(2026, 10, 7, 21, 0, tzinfo=warsaw)), "21:00", "22:10", "Szwagropol"
    )

    assert parse_connections(
        early + late,
        "2026-10-07",
        now=datetime(2026, 10, 6, 10, 0, tzinfo=warsaw),
    ) == [
        {"departure": "05:23", "arrival": "06:40", "carrier": "Maxbus"},
        {"departure": "21:00", "arrival": "22:10", "carrier": "Szwagropol"},
    ]
