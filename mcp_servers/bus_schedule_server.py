"""MCP server for the Rabka-Zdrój ↔ Kraków buses.

`get_bus_schedule` is the original any-route e-podroznik lookup.
`get_bus_departures` answers from the departures board's cache (shared with
the Buses tab), including the time a bus passes a given stop.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from src.bus_departures import STOPS, DepartureBoard, match_stop, validate_request
from src.constants import DATA_DIR
from src.tools.bus_schedule import LOCAL_TIMEZONE, BusScheduleError, get_bus_schedule

server = Server("bus-schedule")
_board = DepartureBoard(os.path.join(DATA_DIR, "cache", "bus_departures"))

_READ_ONLY = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": True,
}


def departures(arguments: dict, board: DepartureBoard = _board) -> dict:
    """Connections of one direction and day, optionally as seen from one stop."""
    direction = arguments.get("direction", "")
    travel_date = arguments.get("date") or datetime.now(LOCAL_TIMEZONE).date().isoformat()
    validate_request(direction, travel_date)
    stop_query = (arguments.get("stop") or "").strip()
    stop_id = match_stop(stop_query) if stop_query else None
    if stop_query and stop_id is None:
        known = ", ".join(stop["label"] for stop in STOPS)
        raise BusScheduleError(f"Nieznany przystanek: {stop_query}. Znane: {known}.")
    departure_filter = (arguments.get("departure") or "").strip()

    result = board.get_ready(direction, travel_date, timeout=40)
    if result["status"] != "ready":
        raise BusScheduleError(result.get("error") or "Rozkład jeszcze się pobiera, spróbuj za chwilę.")

    rows = []
    for connection in result["schedule"]["connections"]:
        stops = connection.get("stops") or []
        times = {stop.get("stop_id"): stop for stop in stops}
        at_stop_time = (times.get(stop_id) or {}).get("departure") if stop_id else None
        if departure_filter and departure_filter not in (connection["departure"], at_stop_time):
            continue
        row = {
            "carrier": connection["carrier"],
            "departure": connection["departure"],
            "from": connection["from_label"],
            "arrival": connection["arrival"],
            "to": connection["to_label"],
        }
        if stop_id:
            if at_stop_time is None:
                continue
            row["at_stop"] = at_stop_time
        if departure_filter:
            row["stops"] = [{"name": s["name"], "time": s["departure"]} for s in stops]
        rows.append(row)

    return {
        "direction": result["schedule"]["to"],
        "date": travel_date,
        "stop": next((s["label"] for s in STOPS if s["id"] == stop_id), None),
        "connections": rows,
        "note": "Godziny rozkładowe z e-podroznik.pl, bez opóźnień na żywo.",
    }


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="get_bus_schedule",
            description=(
                "Return bus connections for a route and date. Use this for bus, "
                "coach, or przewoznik timetable questions."
            ),
            annotations=_READ_ONLY,
            inputSchema={
                "type": "object",
                "properties": {
                    "from_city": {"type": "string", "description": "Departure city"},
                    "to_city": {"type": "string", "description": "Destination city"},
                    "date": {"type": "string", "format": "date", "description": "Travel date in YYYY-MM-DD format"},
                },
                "required": ["from_city", "to_city", "date"],
                "additionalProperties": False,
            },
        ),
        Tool(
            name="get_bus_departures",
            description=(
                "Rabka-Zdrój ↔ Kraków buses (Maxbus, Majerbus, Szwagropol) from the departures "
                "board: every connection of a day, the time it passes a given stop (e.g. AGH, "
                "Jubilat, Myślenice) and, with `departure`, the full stop list of one bus. "
                "Prefer this over get_bus_schedule for Rabka/Kraków questions."
            ),
            annotations=_READ_ONLY,
            inputSchema={
                "type": "object",
                "properties": {
                    "direction": {
                        "type": "string",
                        "enum": ["to_krakow", "to_rabka"],
                        "description": "to_krakow = Rabka → Kraków, to_rabka = Kraków → Rabka",
                    },
                    "date": {"type": "string", "format": "date", "description": "YYYY-MM-DD, default today"},
                    "stop": {"type": "string", "description": "Optional stop name, e.g. 'AGH' or 'Zabornia'"},
                    "departure": {
                        "type": "string",
                        "description": "Optional HH:MM departure (from the first stop or from `stop`) to list that bus's stops",
                    },
                },
                "required": ["direction"],
                "additionalProperties": False,
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        if name == "get_bus_schedule":
            result = await asyncio.to_thread(
                get_bus_schedule,
                arguments.get("from_city", ""),
                arguments.get("to_city", ""),
                arguments.get("date", ""),
            )
        elif name == "get_bus_departures":
            result = await asyncio.to_thread(departures, arguments)
        else:
            raise ValueError(f"Unknown tool: {name}")
    except BusScheduleError as exc:
        result = {"error": str(exc)}
    except ValueError:
        raise
    except Exception:
        result = {"error": "Nie udało się pobrać rozkładu z e-podroznik.pl."}
    return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]


async def main() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
