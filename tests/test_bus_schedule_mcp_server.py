import pytest

from src.bus_departures import DepartureBoard
from src.tools.bus_schedule import BusScheduleError

server = pytest.importorskip("mcp_servers.bus_schedule_server")


def stop(stop_id, time, name=None):
    return {"stop_id": stop_id, "name": name or stop_id, "arrival": time, "departure": time, "date": "2026-10-27"}


def day(direction, travel_date):
    return {
        "direction": direction,
        "date": travel_date,
        "from": "Kraków",
        "to": "Rabka-Zdrój",
        "connections": [
            {
                "id": "06:10-Szwagropol", "carrier": "Szwagropol", "departure": "06:10", "arrival": "07:21",
                "from_label": "Kraków (MDA)", "to_label": "Rabka (Zabornia)",
                "stops": [stop("mda", "06:10", "KRAKÓW MDA"), stop("agh", "06:15", "Kraków AGH/UR"), stop("zabornia", "07:21")],
            },
            {
                "id": "07:40-Szwagropol", "carrier": "Szwagropol", "departure": "07:40", "arrival": "08:53",
                "from_label": "Kraków (MDA)", "to_label": "Rabka (Zabornia)",
                "stops": [stop("mda", "07:40"), stop("zabornia", "08:53")],
            },
        ],
    }


@pytest.fixture
def board(tmp_path):
    return DepartureBoard(tmp_path, fetcher=day)


def test_departures_from_a_stop_skip_buses_that_do_not_stop_there(board):
    result = server.departures({"direction": "to_rabka", "date": "2026-10-27", "stop": "AGH"}, board)

    assert result["stop"] == "Kraków (AGH)"
    assert result["connections"] == [{
        "carrier": "Szwagropol", "departure": "06:10", "from": "Kraków (MDA)",
        "arrival": "07:21", "to": "Rabka (Zabornia)", "at_stop": "06:15",
    }]


def test_departures_with_time_list_that_bus_stops(board):
    result = server.departures({"direction": "to_rabka", "date": "2026-10-27", "departure": "06:15", "stop": "agh"}, board)

    assert [s["name"] for s in result["connections"][0]["stops"]] == ["KRAKÓW MDA", "Kraków AGH/UR", "zabornia"]


def test_departures_reject_unknown_stop(board):
    with pytest.raises(BusScheduleError, match="Nieznany przystanek"):
        server.departures({"direction": "to_rabka", "date": "2026-10-27", "stop": "Wieliczka"}, board)
