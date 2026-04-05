"""
News Calendar.
Fetches Tier 1 economic events and enforces the 2-minute
pre/post blackout window required by MFFU.
"""

import logging
import requests
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# Known Tier 1 events by keyword — used to filter API responses
TIER1_KEYWORDS = [
    "Non-Farm Payroll", "NFP",
    "CPI", "Consumer Price Index",
    "FOMC", "Federal Reserve", "Fed Rate",
    "GDP",
    "PPI", "Producer Price Index",
    "Retail Sales",
    "ISM Manufacturing",
    "ISM Services",
    "Initial Jobless Claims",
    "PCE",
]

BLACKOUT_MINUTES = 2   # minutes before and after event


class NewsCalendar:
    """
    Pulls economic events from Forex Factory (via tradingeconomics or
    a free alternative) and checks for active blackout windows.

    For a clean v1, we use a manually maintained list for the current
    week that gets refreshed daily via the scheduler.
    """

    def __init__(self):
        self._events: list[dict] = []   # [{time: datetime, name: str}]
        self._last_refresh: Optional[datetime] = None

    # ── Public API ─────────────────────────────────────────

    def refresh(self):
        """
        Fetch upcoming Tier 1 events.
        Called once at startup and daily at midnight via APScheduler.
        """
        try:
            events = self._fetch_from_tradingeconomics()
            self._events = events
            self._last_refresh = datetime.now(timezone.utc)
            logger.info(f"News calendar refreshed: {len(events)} Tier 1 events loaded")
        except Exception as e:
            logger.error(f"News calendar refresh failed: {e}")

    def in_blackout(self) -> bool:
        """Return True if we are within BLACKOUT_MINUTES of a Tier 1 event."""
        now = datetime.now(timezone.utc)
        for event in self._events:
            delta = abs((event["time"] - now).total_seconds() / 60)
            if delta <= BLACKOUT_MINUTES:
                return True
        return False

    def event_imminent(self) -> bool:
        """Return True if a Tier 1 event is < 2 minutes away."""
        now = datetime.now(timezone.utc)
        for event in self._events:
            seconds_until = (event["time"] - now).total_seconds()
            if 0 <= seconds_until <= BLACKOUT_MINUTES * 60:
                return True
        return False

    def current_event(self) -> str:
        """Return the name of the active/imminent event, or empty string."""
        now = datetime.now(timezone.utc)
        for event in self._events:
            delta = abs((event["time"] - now).total_seconds() / 60)
            if delta <= BLACKOUT_MINUTES:
                return event["name"]
        return ""

    def next_event(self) -> Optional[dict]:
        """Return the next upcoming Tier 1 event."""
        now = datetime.now(timezone.utc)
        future = [e for e in self._events if e["time"] > now]
        return min(future, key=lambda e: e["time"]) if future else None

    # ── Data fetch ─────────────────────────────────────────

    def _fetch_from_tradingeconomics(self) -> list[dict]:
        """
        Fetch from TradingEconomics calendar API (free tier).
        Falls back to empty list if unavailable.
        Returns list of dicts with 'time' (UTC datetime) and 'name'.
        """
        try:
            resp = requests.get(
                "https://api.tradingeconomics.com/calendar/country/united states",
                params={"c": "guest:guest", "f": "json"},
                timeout=10
            )
            if resp.status_code != 200:
                raise ValueError(f"API returned {resp.status_code}")

            events = []
            for item in resp.json():
                name       = item.get("Event", "")
                importance = item.get("Importance", 0)
                date_str   = item.get("Date", "")

                if not self._is_tier1(name, importance):
                    continue

                try:
                    event_time = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
                    events.append({"time": event_time, "name": name})
                except ValueError:
                    continue

            return events

        except Exception as e:
            logger.warning(f"TradingEconomics fetch failed ({e}) — using empty calendar")
            return []

    def _is_tier1(self, name: str, importance: int) -> bool:
        """Return True if this event qualifies as Tier 1."""
        if importance >= 3:
            return True
        name_upper = name.upper()
        for kw in TIER1_KEYWORDS:
            if kw.upper() in name_upper:
                return True
        return False

    # ── Manual override ────────────────────────────────────

    def add_manual_event(self, event_time: datetime, name: str):
        """Add a manually specified event (e.g. surprise FOMC statement)."""
        self._events.append({"time": event_time, "name": name})
        logger.info(f"Manual event added: {name} at {event_time}")
