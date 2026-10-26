"""Departures board API for the Rabka-Zdrój ↔ Kraków buses."""
import os
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException

from src.bus_departures import STOPS, DepartureBoard, validate_request
from src.constants import DATA_DIR
from src.tools.bus_schedule import LOCAL_TIMEZONE, BusScheduleError


def setup_bus_routes(board: Optional[DepartureBoard] = None):
    board = board or DepartureBoard(os.path.join(DATA_DIR, "cache", "bus_departures"))
    router = APIRouter(prefix="/api/buses", tags=["buses"])

    @router.get("/stops")
    def get_stops():
        return {"stops": [{k: v for k, v in stop.items() if k != "match"} for stop in STOPS]}

    # Plain `def`: FastAPI runs it in the threadpool, so the cache file read
    # never blocks the event loop. Fetching itself happens in board threads.
    @router.get("/schedule")
    def get_schedule(direction: str, date: Optional[str] = None, refresh: bool = False):
        travel_date = date or datetime.now(LOCAL_TIMEZONE).date().isoformat()
        try:
            validate_request(direction, travel_date)
        except BusScheduleError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return board.get(direction, travel_date, refresh=refresh)

    return router
