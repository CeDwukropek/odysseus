"""Send a push notification through the configured ntfy integration.

Mirrors the ntfy branch of routes.note.note_routes.dispatch_reminder (same
integration lookup, topic setting and SSRF guard) for callers outside the
reminder pipeline, such as the bus departures "time to leave" alerts.
"""

from __future__ import annotations

import logging
import os

import requests

logger = logging.getLogger(__name__)


def ntfy_configured() -> bool:
    return _integration() is not None


def _integration() -> dict | None:
    from src.integrations import load_integrations

    return next(
        (i for i in load_integrations()
         if i.get("preset") == "ntfy" and i.get("enabled", True) and i.get("base_url")),
        None,
    )


def push_ntfy(title: str, body: str, *, tags: str = "bell", priority: str = "high") -> tuple[bool, str]:
    """Return (sent, error). Never raises."""
    try:
        from src.settings import load_settings
        from src.url_safety import check_outbound_url

        intg = _integration()
        if intg is None:
            return False, "No enabled ntfy integration"
        base = intg["base_url"].rstrip("/")
        topic = load_settings().get("reminder_ntfy_topic") or "reminders"
        block = os.getenv("REMINDER_WEBHOOK_BLOCK_PRIVATE_IPS", "false").lower() == "true"
        ok, reason = check_outbound_url(f"{base}/{topic}", block_private=block)
        if not ok:
            return False, f"ntfy URL rejected: {reason}"
        # Title as a query parameter rather than the ASCII-only header, so
        # Polish characters survive.
        params = {"title": (title or "Odysseus")[:200], "priority": priority, "tags": tags}
        headers = {}
        if intg.get("api_key"):
            headers["Authorization"] = f'Bearer {intg["api_key"]}'
        response = requests.post(
            f"{base}/{topic}", params=params, data=body.encode("utf-8"), headers=headers, timeout=10
        )
        if not response.ok:
            return False, f"ntfy returned HTTP {response.status_code}"
        return True, ""
    except Exception as exc:  # delivery problems must not kill the caller's loop
        logger.warning("ntfy push failed: %s", exc)
        return False, str(exc) or exc.__class__.__name__
