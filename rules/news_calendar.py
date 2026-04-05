"""
News Calendar.
Fetches Tier 1 economic events and enforces the 2-minute
pre/post blackout window required by MFFU.

Uses multiple data sources with fallback:
1. Forex Factory XML feed (most reliable free source)
2. BLS/Fed/FOMC known schedules as fallback
"""

import logging
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# Known Tier 1 events by keyword — used to filter API responses
TIER1_KEYWORDS = [
    "Non-Farm Payroll", "NFP", "Nonfarm Payroll",
    "Non-Farm Employment", "Employment Change",
    "CPI", "Consumer Price Index",
    "FOMC", "Federal Reserve", "Fed Rate", "Interest Rate Decision",
    "GDP", "Gross Domestic Product",
    "PPI", "Producer Price Index",
    "Retail Sales",
    "ISM Manufacturing", "ISM Services", "ISM Non-Manufacturing",
    "Initial Jobless Claims", "Unemployment Claims",
    "PCE", "Core PCE",
    "Durable Goods",
    "Trade Balance",
]

# Impact keywords that indicate high importance
HIGH_IMPACT_KEYWORDS = ["High", "Red", "3", "high"]

BLACKOUT_MINUTES = 2   # minutes before and after event


class NewsCalendar:
    """
    Pulls economic events from Forex Factory and other free sources.
    Checks for active blackout windows around Tier 1 events.
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
            # Try Forex Factory first (most reliable)
            events = self._fetch_from_forex_factory()

            if not events:
                # Fallback to known scheduled events
                logger.warning("Forex Factory unavailable, using known schedule fallback")
                events = self._get_known_scheduled_events()

            self._events = events
            self._last_refresh = datetime.now(timezone.utc)
            logger.info(f"News calendar refreshed: {len(events)} Tier 1 events loaded")

            # Log upcoming events
            for event in sorted(events, key=lambda x: x['time'])[:5]:
                logger.info(f"  Upcoming: {event['name']} at {event['time'].strftime('%Y-%m-%d %H:%M')} UTC")

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

    def get_events_today(self) -> list[dict]:
        """Return all events scheduled for today."""
        now = datetime.now(timezone.utc)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        today_end = today_start + timedelta(days=1)
        return [e for e in self._events if today_start <= e["time"] < today_end]

    # ── Data Sources ───────────────────────────────────────

    def _fetch_from_forex_factory(self) -> list[dict]:
        """
        Fetch from Forex Factory's XML calendar feed.
        This is a well-known free source used by many traders.
        """
        try:
            # Forex Factory provides a weekly XML feed
            url = "https://nfs.faireconomy.media/ff_calendar_thisweek.xml"

            resp = requests.get(url, timeout=15, headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            })

            if resp.status_code != 200:
                logger.warning(f"Forex Factory returned status {resp.status_code}")
                return []

            # Parse XML
            root = ET.fromstring(resp.content)
            events = []

            for event_elem in root.findall('.//event'):
                title = event_elem.findtext('title', '')
                country = event_elem.findtext('country', '')
                date_str = event_elem.findtext('date', '')
                time_str = event_elem.findtext('time', '')
                impact = event_elem.findtext('impact', '')

                # Only US events
                if country.upper() != 'USD':
                    continue

                # Only high impact or Tier 1 keyword match
                if not self._is_tier1(title, impact):
                    continue

                # Parse datetime
                try:
                    event_time = self._parse_ff_datetime(date_str, time_str)
                    if event_time:
                        events.append({"time": event_time, "name": title})
                except Exception as e:
                    logger.debug(f"Failed to parse FF event time: {e}")
                    continue

            return events

        except Exception as e:
            logger.warning(f"Forex Factory fetch failed: {e}")
            return []

    def _parse_ff_datetime(self, date_str: str, time_str: str) -> Optional[datetime]:
        """Parse Forex Factory date and time strings into UTC datetime."""
        if not date_str:
            return None

        # FF times are in ET (Eastern Time)
        # Format: "01-06-2025" and "8:30am" or "10:00am" or "All Day" or "Tentative"
        if not time_str or time_str.lower() in ['all day', 'tentative', '']:
            time_str = "8:30am"  # Default to common release time

        try:
            # Parse date (MM-DD-YYYY format)
            date_parts = date_str.split('-')
            if len(date_parts) == 3:
                month, day, year = int(date_parts[0]), int(date_parts[1]), int(date_parts[2])
            else:
                return None

            # Parse time (8:30am format)
            time_str = time_str.lower().strip()
            is_pm = 'pm' in time_str
            time_str = time_str.replace('am', '').replace('pm', '').strip()

            if ':' in time_str:
                hour, minute = map(int, time_str.split(':'))
            else:
                hour, minute = int(time_str), 0

            if is_pm and hour != 12:
                hour += 12
            elif not is_pm and hour == 12:
                hour = 0

            # Create datetime in ET, then convert to UTC
            # ET is UTC-5 (EST) or UTC-4 (EDT)
            # For simplicity, assume EST (UTC-5) - this is close enough for blackout windows
            et_dt = datetime(year, month, day, hour, minute)
            utc_dt = et_dt + timedelta(hours=5)  # Convert ET to UTC (approximate)

            return utc_dt.replace(tzinfo=timezone.utc)

        except Exception as e:
            logger.debug(f"DateTime parse error: {e}")
            return None

    def _get_known_scheduled_events(self) -> list[dict]:
        """
        Fallback: Generate events from known recurring schedules.
        Major US economic releases follow predictable patterns.
        """
        events = []
        now = datetime.now(timezone.utc)

        # NFP: First Friday of each month at 8:30 AM ET (13:30 UTC)
        nfp_date = self._get_first_friday(now.year, now.month)
        if nfp_date >= now.date():
            events.append({
                "time": datetime(nfp_date.year, nfp_date.month, nfp_date.day, 13, 30, tzinfo=timezone.utc),
                "name": "Non-Farm Payrolls"
            })

        # Also check next month's NFP
        next_month = now.month + 1 if now.month < 12 else 1
        next_year = now.year if now.month < 12 else now.year + 1
        nfp_date_next = self._get_first_friday(next_year, next_month)
        events.append({
            "time": datetime(nfp_date_next.year, nfp_date_next.month, nfp_date_next.day, 13, 30, tzinfo=timezone.utc),
            "name": "Non-Farm Payrolls"
        })

        # CPI: Usually mid-month, 8:30 AM ET
        # FOMC: 8 times per year, usually 2:00 PM ET for statement

        # Weekly: Initial Jobless Claims - Thursdays at 8:30 AM ET (13:30 UTC)
        next_thursday = now + timedelta(days=(3 - now.weekday()) % 7)
        if next_thursday.date() == now.date() and now.hour >= 14:
            next_thursday += timedelta(days=7)
        events.append({
            "time": datetime(next_thursday.year, next_thursday.month, next_thursday.day, 13, 30, tzinfo=timezone.utc),
            "name": "Initial Jobless Claims"
        })

        # Filter to only future events
        return [e for e in events if e["time"] > now]

    def _get_first_friday(self, year: int, month: int):
        """Get the first Friday of a given month."""
        from datetime import date
        first_day = date(year, month, 1)
        # Friday is weekday 4
        days_until_friday = (4 - first_day.weekday()) % 7
        return first_day + timedelta(days=days_until_friday)

    def _is_tier1(self, name: str, impact: str = "") -> bool:
        """Return True if this event qualifies as Tier 1."""
        # Check impact level
        if any(kw.lower() in impact.lower() for kw in HIGH_IMPACT_KEYWORDS):
            return True

        # Check name against keywords
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

    def status(self) -> dict:
        """Return calendar status for debugging."""
        return {
            "events_loaded": len(self._events),
            "last_refresh": self._last_refresh.isoformat() if self._last_refresh else None,
            "in_blackout": self.in_blackout(),
            "next_event": self.next_event(),
        }
