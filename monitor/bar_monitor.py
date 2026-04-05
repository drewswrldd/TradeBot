"""
Bar Confirmation Monitor.
ATS Strategy 1 entry rule:
  Long:  enter when the NEXT bar's high exceeds the trigger bar's high
  Short: enter when the NEXT bar's low  drops below the trigger bar's low

This module holds the pending signal and watches the live price feed
until confirmation fires, then calls the agent's on_entry_confirmed().
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Optional

logger = logging.getLogger(__name__)


@dataclass
class PendingSignal:
    direction:     str      # 'long' | 'short'
    trigger_high:  float    # high of the bar that caused ATS color change
    trigger_low:   float    # low  of the bar that caused ATS color change
    atr:           float    # ATR value for calculating stop distance
    ats_bar_time:  str      # ISO timestamp of the trigger bar
    received_at:   datetime = field(default_factory=datetime.utcnow)
    confirmed:     bool     = False

    @property
    def entry_trigger_price(self) -> float:
        """Price that must be exceeded to confirm entry."""
        return self.trigger_high if self.direction == "long" else self.trigger_low


class BarConfirmationMonitor:
    """
    Holds up to one pending ATS signal at a time.
    On every price tick, checks if the confirmation condition is met.
    If confirmed, fires on_entry_confirmed callback and clears the signal.
    """

    def __init__(self, on_entry_confirmed: Callable):
        self._pending:             Optional[PendingSignal] = None
        self.on_entry_confirmed:   Callable = on_entry_confirmed
        self._last_tick:           float    = 0.0

    # ── Signal intake ──────────────────────────────────────

    def set_signal(self, signal: PendingSignal):
        """
        Store an incoming ATS signal.
        Replaces any existing unconfirmed signal (ATS reversed before entry).
        """
        if self._pending and not self._pending.confirmed:
            logger.info(
                f"Replacing unconfirmed {self._pending.direction} signal "
                f"with new {signal.direction} signal"
            )
        self._pending = signal
        logger.info(
            f"Pending signal set: {signal.direction.upper()} | "
            f"Entry trigger: {signal.entry_trigger_price} | "
            f"ATR: {signal.atr}"
        )

    def clear(self):
        """Clear pending signal (e.g. on ATS color reversal before entry)."""
        if self._pending:
            logger.info(f"Pending {self._pending.direction} signal cleared")
        self._pending = None

    def has_pending(self) -> bool:
        return self._pending is not None and not self._pending.confirmed

    def pending_direction(self) -> Optional[str]:
        return self._pending.direction if self._pending else None

    # ── Tick handler ───────────────────────────────────────

    def on_tick(self, price: float):
        """
        Called on every live price tick from the WebSocket.
        Checks if the pending signal's confirmation condition is met.
        """
        self._last_tick = price

        if not self._pending or self._pending.confirmed:
            return

        signal = self._pending
        confirmed = False

        if signal.direction == "long":
            # Long confirmed: price trades ABOVE trigger bar's high
            if price > signal.trigger_high:
                confirmed = True
                entry_price = price   # fill at current market

        elif signal.direction == "short":
            # Short confirmed: price trades BELOW trigger bar's low
            if price < signal.trigger_low:
                confirmed = True
                entry_price = price

        if confirmed:
            signal.confirmed = True
            logger.info(
                f"ENTRY CONFIRMED: {signal.direction.upper()} @ {entry_price:.2f} | "
                f"Trigger was {signal.entry_trigger_price:.2f}"
            )
            self._pending = None
            self.on_entry_confirmed(signal, entry_price)

    # ── Status ─────────────────────────────────────────────

    def status(self) -> dict:
        if not self._pending:
            return {"pending": False}
        s = self._pending
        return {
            "pending":       True,
            "direction":     s.direction,
            "trigger_price": s.entry_trigger_price,
            "atr":           s.atr,
            "last_tick":     self._last_tick,
            "gap_to_entry":  round(abs(self._last_tick - s.entry_trigger_price), 2),
            "received_at":   s.received_at.isoformat(),
        }
