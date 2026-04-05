"""
Position Monitor.
Watches open MES positions and fires exit logic for Strategy 1:
  - 50% exit at 2R profit
  - Remaining 50% exit when ATS turns the opposite color
Also calls the MFFU rules engine's emergency flatten check on every tick.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Optional
from rules.risk import calculate_targets, round_to_tick

logger = logging.getLogger(__name__)


@dataclass
class OpenTrade:
    direction:        str     # 'long' | 'short'
    entry_price:      float
    stop_price:       float
    total_contracts:  int
    remaining_contracts: int  = 0
    target_2r:        float   = 0.0
    partial_exited:   bool    = False
    entry_time:       datetime = field(default_factory=datetime.utcnow)
    entry_order_id:   Optional[int] = None
    stop_order_id:    Optional[int] = None

    def __post_init__(self):
        self.remaining_contracts = self.total_contracts
        targets = calculate_targets(self.entry_price, self.stop_price, self.direction)
        self.target_2r = targets["target_2r"]

    @property
    def r_value(self) -> float:
        return abs(self.entry_price - self.stop_price)

    @property
    def contracts_for_partial(self) -> int:
        """50% of position, minimum 1."""
        return max(1, self.total_contracts // 2)

    @property
    def contracts_remaining_after_partial(self) -> int:
        return self.total_contracts - self.contracts_for_partial


class PositionMonitor:
    """
    Watches the open trade tick by tick.
    Fires callbacks for:
      - on_partial_exit: when price hits 2R (sell half)
      - on_full_exit:    when ATS reverses color or emergency flatten
    """

    def __init__(self,
                 on_partial_exit: Callable,
                 on_full_exit:    Callable,
                 rules_engine=None):
        self._trade:         Optional[OpenTrade] = None
        self.on_partial_exit = on_partial_exit
        self.on_full_exit    = on_full_exit
        self.rules_engine    = rules_engine

    # ── Trade lifecycle ────────────────────────────────────

    def open_trade(self, trade: OpenTrade):
        self._trade = trade
        logger.info(
            f"Position monitor: tracking {trade.direction.upper()} "
            f"{trade.total_contracts} MES @ {trade.entry_price} | "
            f"Stop: {trade.stop_price} | 2R target: {trade.target_2r}"
        )

    def close_trade(self, reason: str):
        if self._trade:
            logger.info(f"Trade closed ({reason}): {self._trade.direction.upper()} "
                        f"@ entry {self._trade.entry_price}")
        self._trade = None

    def has_open_trade(self) -> bool:
        return self._trade is not None

    def get_trade(self) -> Optional[OpenTrade]:
        return self._trade

    # ── Tick handler ───────────────────────────────────────

    def on_tick(self, price: float):
        """
        Called on every live price tick.
        Checks 2R target and emergency flatten conditions.
        """
        if not self._trade:
            return

        trade = self._trade

        # ── Emergency flatten check (MFFU rules) ──
        if self.rules_engine:
            should_flatten, reason = self.rules_engine.should_emergency_flatten()
            if should_flatten:
                logger.warning(f"EMERGENCY FLATTEN triggered: {reason}")
                self.on_full_exit(trade, price, f"EMERGENCY: {reason}")
                self.close_trade("emergency_flatten")
                return

        # ── 2R partial exit ──
        if not trade.partial_exited:
            hit_2r = (
                (trade.direction == "long"  and price >= trade.target_2r) or
                (trade.direction == "short" and price <= trade.target_2r)
            )
            if hit_2r:
                logger.info(
                    f"2R target hit @ {price:.2f} (target: {trade.target_2r:.2f}) — "
                    f"exiting {trade.contracts_for_partial} contracts"
                )
                trade.partial_exited = True
                trade.remaining_contracts = trade.contracts_remaining_after_partial
                self.on_partial_exit(trade, price)

    # ── ATS reversal exit ──────────────────────────────────

    def on_ats_reversal(self, new_color: str, bar_close_price: float):
        """
        Called by the webhook handler when ATS changes color.
        Strategy 1: exit remaining contracts at the close of the reversal bar.
        """
        if not self._trade:
            return

        trade = self._trade
        reversal_is_against_trade = (
            (trade.direction == "long"  and new_color == "red")  or
            (trade.direction == "short" and new_color == "blue")
        )

        if reversal_is_against_trade:
            remaining = trade.remaining_contracts
            logger.info(
                f"ATS reversed to {new_color.upper()} — exiting {remaining} remaining contracts "
                f"@ {bar_close_price:.2f}"
            )
            self.on_full_exit(trade, bar_close_price, f"ATS reversal to {new_color}")
            self.close_trade("ats_reversal")

    # ── Status ─────────────────────────────────────────────

    def status(self) -> dict:
        if not self._trade:
            return {"open_trade": False}
        t = self._trade
        return {
            "open_trade":         True,
            "direction":          t.direction,
            "entry_price":        t.entry_price,
            "stop_price":         t.stop_price,
            "target_2r":          t.target_2r,
            "total_contracts":    t.total_contracts,
            "remaining_contracts": t.remaining_contracts,
            "partial_exited":     t.partial_exited,
            "entry_time":         t.entry_time.isoformat(),
        }
