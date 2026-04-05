"""
MFFU Rules Engine.
Hard gate between the AI agent's decisions and order execution.
Every proposed order must pass check_pre_trade() before going live.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, date
from typing import Optional
from config import (
    ACCOUNT_SIZE, MAX_DRAWDOWN, PROFIT_TARGET,
    MAX_CONTRACTS, MES_POINT_VALUE
)

logger = logging.getLogger(__name__)


@dataclass
class AccountState:
    """Live snapshot of the account, updated from Tradovate on every fill."""
    starting_balance: float   = ACCOUNT_SIZE
    high_water_mark:  float   = ACCOUNT_SIZE
    current_balance:  float   = ACCOUNT_SIZE
    realized_pnl:     float   = 0.0
    open_pnl:         float   = 0.0

    # per-cycle tracking (resets after each payout)
    cycle_profit:     float   = 0.0
    daily_profits:    dict    = field(default_factory=dict)  # date → float

    @property
    def total_equity(self) -> float:
        return self.current_balance + self.open_pnl

    @property
    def drawdown_used(self) -> float:
        """How much of the max drawdown has been consumed."""
        return self.high_water_mark - self.current_balance

    @property
    def drawdown_remaining(self) -> float:
        return MAX_DRAWDOWN - self.drawdown_used

    @property
    def today_profit(self) -> float:
        return self.daily_profits.get(date.today().isoformat(), 0.0)

    def update_high_water_mark(self):
        if self.current_balance > self.high_water_mark:
            self.high_water_mark = self.current_balance
            logger.info(f"New high-water mark: ${self.high_water_mark:,.2f}")


@dataclass
class RuleViolation:
    rule:    str
    message: str
    blocked: bool = True   # True = hard block, False = warning only


class MFFURulesEngine:
    """
    Enforces all MFFU 50K Flex evaluation rules.

    Rules hardcoded:
    - Max trailing drawdown: $2,000 (EOD, from high-water mark)
    - Profit target: $3,000
    - Max contracts: configured in .env (default 3)
    - Consistency: no single day > 50% of profit target ($1,500)
    - News blackout: flat 2 min before/after Tier 1 events
    - Max 200 trades/day
    """

    def __init__(self, news_calendar=None):
        self.state         = AccountState()
        self.news_calendar = news_calendar   # injected news_calendar.py instance
        self.trade_count_today = 0
        self.last_trade_date   = None

    # ── Main gate ──────────────────────────────────────────

    def check_pre_trade(self, proposed_contracts: int,
                         stop_distance_points: float,
                         action: str) -> tuple[bool, list[RuleViolation]]:
        """
        Call this BEFORE placing any order.
        Returns (approved: bool, violations: list[RuleViolation])
        """
        violations = []
        self._reset_daily_counter_if_needed()

        # 1. Drawdown floor check
        v = self._check_drawdown(stop_distance_points, proposed_contracts)
        if v:
            violations.append(v)

        # 2. Contract limit
        v = self._check_contract_limit(proposed_contracts)
        if v:
            violations.append(v)

        # 3. Consistency rule
        v = self._check_consistency()
        if v:
            violations.append(v)

        # 4. News blackout
        v = self._check_news_blackout()
        if v:
            violations.append(v)

        # 5. Daily trade count
        v = self._check_trade_count()
        if v:
            violations.append(v)

        # 6. Profit target already hit
        v = self._check_profit_target_hit()
        if v:
            violations.append(v)

        hard_blocked = any(v.blocked for v in violations)
        if violations:
            for v in violations:
                level = "BLOCKED" if v.blocked else "WARNING"
                logger.warning(f"[{level}] {v.rule}: {v.message}")

        return (not hard_blocked), violations

    # ── Auto-flatten trigger ───────────────────────────────

    def should_emergency_flatten(self) -> tuple[bool, str]:
        """
        Returns (True, reason) if positions must be closed immediately.
        Called on every tick by the position monitor.
        """
        # Drawdown within $100 of the floor
        if self.state.drawdown_remaining <= 100:
            return True, f"Drawdown critical: ${self.state.drawdown_remaining:.2f} remaining"

        # Total equity approaching floor
        floor = self.state.high_water_mark - MAX_DRAWDOWN
        if self.state.total_equity <= floor + 50:
            return True, f"Equity ${self.state.total_equity:.2f} near floor ${floor:.2f}"

        # News event starting in < 2 minutes with open position
        if self.news_calendar and self.news_calendar.event_imminent():
            return True, "Tier 1 news event imminent — flattening"

        return False, ""

    # ── State updates ──────────────────────────────────────

    def on_fill(self, realized_pnl_delta: float):
        """Call after every fill to keep state current."""
        self.state.realized_pnl  += realized_pnl_delta
        self.state.current_balance += realized_pnl_delta
        self.state.cycle_profit  += realized_pnl_delta
        today = date.today().isoformat()
        self.state.daily_profits[today] = \
            self.state.daily_profits.get(today, 0.0) + realized_pnl_delta
        self.state.update_high_water_mark()
        self.trade_count_today += 1
        logger.info(
            f"Fill recorded: Δ${realized_pnl_delta:.2f} | "
            f"Balance: ${self.state.current_balance:,.2f} | "
            f"Drawdown remaining: ${self.state.drawdown_remaining:.2f}"
        )

    def update_open_pnl(self, open_pnl: float):
        """Update mark-to-market P&L from WebSocket ticks."""
        self.state.open_pnl = open_pnl

    def sync_from_tradovate(self, account_summary: dict):
        """Sync state from Tradovate account summary."""
        self.state.current_balance = account_summary.get("cash_balance", self.state.current_balance)
        self.state.realized_pnl    = account_summary.get("realized_pnl", self.state.realized_pnl)
        self.state.open_pnl        = account_summary.get("open_pnl", self.state.open_pnl)
        self.state.update_high_water_mark()

    # ── Individual rule checks ─────────────────────────────

    def _check_drawdown(self, stop_pts: float, contracts: int) -> Optional[RuleViolation]:
        max_loss_this_trade = stop_pts * MES_POINT_VALUE * contracts
        if max_loss_this_trade > self.state.drawdown_remaining - 100:
            return RuleViolation(
                rule="DRAWDOWN",
                message=(
                    f"Trade max loss ${max_loss_this_trade:.2f} would breach drawdown floor. "
                    f"Remaining: ${self.state.drawdown_remaining:.2f}"
                ),
                blocked=True,
            )
        return None

    def _check_contract_limit(self, contracts: int) -> Optional[RuleViolation]:
        if contracts > MAX_CONTRACTS:
            return RuleViolation(
                rule="CONTRACT_LIMIT",
                message=f"Requested {contracts} contracts exceeds max {MAX_CONTRACTS}",
                blocked=True,
            )
        return None

    def _check_consistency(self) -> Optional[RuleViolation]:
        """No single day can be > 50% of profit target ($1,500 on $3K target)."""
        max_day = PROFIT_TARGET * 0.50
        if self.state.today_profit >= max_day:
            return RuleViolation(
                rule="CONSISTENCY",
                message=(
                    f"Today's profit ${self.state.today_profit:.2f} has reached "
                    f"50% consistency limit ${max_day:.2f}. No more trades today."
                ),
                blocked=True,
            )
        return None

    def _check_news_blackout(self) -> Optional[RuleViolation]:
        if self.news_calendar and self.news_calendar.in_blackout():
            event = self.news_calendar.current_event()
            return RuleViolation(
                rule="NEWS_BLACKOUT",
                message=f"Tier 1 news blackout active: {event}",
                blocked=True,
            )
        return None

    def _check_trade_count(self) -> Optional[RuleViolation]:
        if self.trade_count_today >= 200:
            return RuleViolation(
                rule="TRADE_LIMIT",
                message=f"200 trade/day limit reached ({self.trade_count_today} today)",
                blocked=True,
            )
        return None

    def _check_profit_target_hit(self) -> Optional[RuleViolation]:
        if self.state.cycle_profit >= PROFIT_TARGET:
            return RuleViolation(
                rule="PROFIT_TARGET_HIT",
                message=(
                    f"Profit target ${PROFIT_TARGET:,.0f} reached "
                    f"(${self.state.cycle_profit:,.2f}). Stop trading and request payout."
                ),
                blocked=True,
            )
        return None

    def _reset_daily_counter_if_needed(self):
        today = date.today()
        if self.last_trade_date != today:
            self.trade_count_today = 0
            self.last_trade_date   = today

    # ── Status summary ─────────────────────────────────────

    def status(self) -> dict:
        return {
            "balance":            self.state.current_balance,
            "high_water_mark":    self.state.high_water_mark,
            "drawdown_used":      self.state.drawdown_used,
            "drawdown_remaining": self.state.drawdown_remaining,
            "today_profit":       self.state.today_profit,
            "cycle_profit":       self.state.cycle_profit,
            "profit_target":      PROFIT_TARGET,
            "pct_to_target":      round(self.state.cycle_profit / PROFIT_TARGET * 100, 1),
            "trades_today":       self.trade_count_today,
        }
