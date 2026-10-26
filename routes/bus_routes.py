"""Departures board API for the Rabka-Zdrój ↔ Kraków buses."""
import os
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from src.auth_helpers import get_current_user
from src.bus_departures import STOPS, DepartureBoard, LeaveAlerts, validate_request
from src.constants import DATA_DIR
from src.ntfy_push import ntfy_configured, push_ntfy
from src.tools.bus_schedule import LOCAL_TIMEZONE, BusScheduleError


class LeaveAlertRequest(BaseModel):
    date: str
    connection_id: str
    stop_id: str
    leave_at: str
    title: str = ""
    body: str = ""


def setup_bus_routes(
    board: Optional[DepartureBoard] = None,
    alerts: Optional[LeaveAlerts] = None,
    *,
    notifier_ready=ntfy_configured,
):
    if board is None:
        board = DepartureBoard(os.path.join(DATA_DIR, "cache", "bus_departures"))
        board.start_warmer()
    if alerts is None:
        alerts = LeaveAlerts(os.path.join(DATA_DIR, "bus_leave_alerts.json"), sender=push_ntfy)
        alerts.start()
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

    @router.get("/alerts")
    def list_alerts(request: Request):
        owner = get_current_user(request) or ""
        return {"alerts": alerts.for_owner(owner), "push_ready": notifier_ready()}

    @router.post("/alerts")
    def add_alert(body: LeaveAlertRequest, request: Request):
        owner = get_current_user(request) or ""
        try:
            record = alerts.add(owner, body.model_dump())
        except (BusScheduleError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {"alert": record, "push_ready": notifier_ready()}

    @router.delete("/alerts/{alert_id}")
    def delete_alert(alert_id: str, request: Request):
        owner = get_current_user(request) or ""
        if not alerts.remove(owner, alert_id):
            raise HTTPException(status_code=404, detail="Nie ma takiego przypomnienia.")
        return {"ok": True}

    return router
