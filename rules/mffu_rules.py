"""
MFFU Rules Engine.
Hard gate between the AI agent's decisions and order execution.
Every proposed order must pass check_pre_trade() before going live.

SAFETY RULESET:
- Daily loss limit: $500 → block all trades for rest of day
- Daily profit target: $300 → lock in gains, stop trading for day
- Trailing profit protection: if profit was $200+ and drops $100 from peak → stop for day
- Weekly profit target: $1,000 → reduce max contracts to 1
- Internal trailing drawdown: $1,500 → flatten all, SHUTDOWN permanently (CHALLENGE_BLOWN)
"""

import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, date, timedelta
from typing import Optional
from pathlib import Path

from config import (
    ACCOUNT_SIZE, MAX_DRAWDOWN, PROFIT_TARGET, MAX_CONTRACTS, MES_POINT_VALUE,
    DAILY_LOSS_LIMIT, DAILY_PROFIT_TARGET, WEEKLY_PROFIT_TARGET,
    INTERNAL_MAX_DRAWDOWN, TRAILING_DRAWDOWN_SHUTDOWN,
)

logger = logging.getLogger(__name__)

# Database path for bot state persistence
BOT_STATE_DB = Path(__file__).parent.parent / "data" / "bot_state.db"


def _init_bot_state_db():
    """Initialize bot state database for persistent flags."""
    BOT_STATE_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(BOT_STATE_DB))
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS bot_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()


def get_bot_state(key: str) -> Optional[str]:
    """Get a persistent bot state value."""
    try:
        conn = sqlite3.connect(str(BOT_STATE_DB))
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM bot_state WHERE key = ?", (key,))
        row = cursor.fetchone()
        conn.close()
        return row[0] if row else None
    except Exception as e:
        logger.error(f"Failed to get bot state {key}: {e}")
        return None


def set_bot_state(key: str, value: str):
    """Set a persistent bot state value."""
    try:
        conn = sqlite3.connect(str(BOT_STATE_DB))
        cursor = conn.cursor()
        cursor.execute("""
            INSERT OR REPLACE INTO bot_state (key, value, updated_at)
            VALUES (?, ?, ?)
        """, (key, value, datetime.utcnow().isoformat()))
        conn.commit()
        conn.close()
        logger.info(f"Bot state set: {key} = {value}")
    except Exception as e:
        logger.error(f"Failed to set bot state {key}: {e}")


# Initialize database on module load
_init_bot_state_db()


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

    # NEW: Daily peak profit tracking for trailing protection
    daily_peak_profit: dict   = field(default_factory=dict)  # date → float

    # NEW: Weekly profit tracking (resets Monday midnight)
    week_start_balance: float = ACCOUNT_SIZE
    week_start_date:    str   = ""

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
    def internal_drawdown_remaining(self) -> float:
        """Internal stricter drawdown limit."""
        return INTERNAL_MAX_DRAWDOWN - self.drawdown_used

    @property
    def today_profit(self) -> float:
        return self.daily_profits.get(date.today().isoformat(), 0.0)

    @property
    def today_peak_profit(self) -> float:
        return self.daily_peak_profit.get(date.today().isoformat(), 0.0)

    @property
    def weekly_profit(self) -> float:
        """Profit since Monday midnight."""
        return self.current_balance - self.week_start_balance

    def update_high_water_mark(self):
        if self.current_balance > self.high_water_mark:
            self.high_water_mark = self.current_balance
            logger.info(f"New high-water mark: ${self.high_water_mark:,.2f}")

    def update_daily_peak(self):
        """Update today's peak profit if current profit is higher."""
        today = date.today().isoformat()
        current_profit = self.today_profit
        current_peak = self.daily_peak_profit.get(today, 0.0)
        if current_profit > current_peak:
            self.daily_peak_profit[today] = current_profit
            logger.info(f"New daily peak profit: ${current_profit:.2f}")

    def reset_week_if_needed(self):
        """Reset weekly tracking on Monday."""
        today = date.today()
        # Get Monday of current week
        monday = today - timedelta(days=today.weekday())
        monday_str = monday.isoformat()

        if self.week_start_date != monday_str:
            self.week_start_balance = self.current_balance
            self.week_start_date = monday_str
            logger.info(f"Weekly tracking reset. New week start balance: ${self.week_start_balance:,.2f}")


@dataclass
class RuleViolation:
    rule:    str
    message: str
    blocked: bool = True   # True = hard block, False = warning only


class MFFURulesEngine:
    """
    Enforces all MFFU 50K Flex evaluation rules PLUS internal safety limits.

    MFFU Rules:
    - Max trailing drawdown: $2,000 (EOD, from high-water mark)
    - Profit target: $3,000
    - Max contracts: configured in .env (default 2)
    - Consistency: no single day > 50% of profit target ($1,500)
    - News blackout: flat 2 min before/after Tier 1 events
    - Max 200 trades/day

    INTERNAL SAFETY RULES:
    - Daily loss limit: $500 → block all trades for rest of day
    - Daily profit target: $300 → lock in gains for day
    - Trailing profit protection: $200+ peak, $100 giveback → stop for day
    - Weekly profit target: $1,000 → reduce to 1 contract
    - Internal trailing drawdown: $1,500 → SHUTDOWN (CHALLENGE_BLOWN)
    """

    def __init__(self, news_calendar=None):
        self.state         = AccountState()
        self.news_calendar = news_calendar   # injected news_calendar.py instance
        self.trade_count_today = 0
        self.last_trade_date   = None

        # Daily lockout flags (reset at midnight)
        self._daily_loss_lockout   = False
        self._daily_profit_lockout = False
        self._trailing_profit_lockout = False
        self._lockout_date = None

        # Check if already blown
        self._challenge_blown = get_bot_state("CHALLENGE_BLOWN") == "True"
        if self._challenge_blown:
            logger.critical("BOT LOADED WITH CHALLENGE_BLOWN FLAG SET — ALL TRADING DISABLED")

    # ── Main gate ──────────────────────────────────────────

    def check_pre_trade(self, proposed_contracts: int,
                         stop_distance_points: float,
                         action: str) -> tuple[bool, list[RuleViolation]]:
        """
        Call this BEFORE placing any order.
        Returns (approved: bool, violations: list[RuleViolation])
        """
        violations = []
        self._reset_daily_lockouts_if_needed()
        self.state.reset_week_if_needed()

        # 0. CRITICAL: Check if challenge is blown (permanent shutdown)
        if self._challenge_blown:
            violations.append(RuleViolation(
                rule="CHALLENGE_BLOWN",
                message="INTERNAL DRAWDOWN LIMIT HIT — BOT SHUTDOWN. Manual intervention required.",
                blocked=True,
            ))
            return False, violations

        # 1. Daily loss limit check
        v = self._check_daily_loss_limit()
        if v:
            violations.append(v)

        # 2. Daily profit target check
        v = self._check_daily_profit_target()
        if v:
            violations.append(v)

        # 3. Trailing profit protection
        v = self._check_trailing_profit_protection()
        if v:
            violations.append(v)

        # 4. Internal drawdown check (stricter than MFFU)
        v = self._check_internal_drawdown(stop_distance_points, proposed_contracts)
        if v:
            violations.append(v)

        # 5. Drawdown floor check (MFFU limit)
        v = self._check_drawdown(stop_distance_points, proposed_contracts)
        if v:
            violations.append(v)

        # 6. Contract limit (may be reduced by weekly profit target)
        effective_max = self._get_effective_max_contracts()
        v = self._check_contract_limit(proposed_contracts, effective_max)
        if v:
            violations.append(v)

        # 7. Consistency rule
        v = self._check_consistency()
        if v:
            violations.append(v)

        # 8. News blackout
        v = self._check_news_blackout()
        if v:
            violations.append(v)

        # 9. Daily trade count
        v = self._check_trade_count()
        if v:
            violations.append(v)

        # 10. MFFU Profit target already hit
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
        # CRITICAL: Internal drawdown limit breach → permanent shutdown
        if self.state.internal_drawdown_remaining <= 0:
            self._trigger_challenge_blown()
            return True, "INTERNAL DRAWDOWN LIMIT HIT — BOT SHUTDOWN"

        # Drawdown within $100 of the MFFU floor
        if self.state.drawdown_remaining <= 100:
            return True, f"Drawdown critical: ${self.state.drawdown_remaining:.2f} remaining"

        # Internal drawdown within $50 → emergency flatten
        if self.state.internal_drawdown_remaining <= 50:
            return True, f"Internal drawdown critical: ${self.state.internal_drawdown_remaining:.2f} remaining"

        # Total equity approaching floor
        floor = self.state.high_water_mark - MAX_DRAWDOWN
        if self.state.total_equity <= floor + 50:
            return True, f"Equity ${self.state.total_equity:.2f} near floor ${floor:.2f}"

        # News event starting in < 2 minutes with open position
        if self.news_calendar and self.news_calendar.event_imminent():
            return True, "Tier 1 news event imminent — flattening"

        return False, ""

    def _trigger_challenge_blown(self):
        """Trigger permanent shutdown due to internal drawdown breach."""
        if not self._challenge_blown:
            self._challenge_blown = True
            set_bot_state("CHALLENGE_BLOWN", "True")
            logger.critical("=" * 60)
            logger.critical("INTERNAL DRAWDOWN LIMIT HIT — BOT SHUTDOWN")
            logger.critical(f"High water mark: ${self.state.high_water_mark:,.2f}")
            logger.critical(f"Current balance: ${self.state.current_balance:,.2f}")
            logger.critical(f"Drawdown: ${self.state.drawdown_used:,.2f}")
            logger.critical("CHALLENGE_BLOWN flag set in database")
            logger.critical("Manual intervention required to restart trading")
            logger.critical("=" * 60)

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
        self.state.update_daily_peak()
        self.trade_count_today += 1

        # Check if internal drawdown breached
        if self.state.internal_drawdown_remaining <= 0 and TRAILING_DRAWDOWN_SHUTDOWN:
            self._trigger_challenge_blown()

        logger.info(
            f"Fill recorded: Δ${realized_pnl_delta:.2f} | "
            f"Balance: ${self.state.current_balance:,.2f} | "
            f"Drawdown remaining: ${self.state.drawdown_remaining:.2f} | "
            f"Internal DD remaining: ${self.state.internal_drawdown_remaining:.2f}"
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
        self.state.reset_week_if_needed()

    # ── NEW: Daily/Weekly safety rule checks ───────────────

    def _check_daily_loss_limit(self) -> Optional[RuleViolation]:
        """Block trading if today's losses exceed $500."""
        if self._daily_loss_lockout:
            return RuleViolation(
                rule="DAILY_LOSS_LIMIT",
                message="Daily loss limit hit — no more trades today",
                blocked=True,
            )
        if self.state.today_profit <= -DAILY_LOSS_LIMIT:
            self._daily_loss_lockout = True
            self._lockout_date = date.today()
            logger.warning(f"Daily loss limit hit — no more trades today (loss: ${abs(self.state.today_profit):.2f})")
            return RuleViolation(
                rule="DAILY_LOSS_LIMIT",
                message=f"Daily loss limit hit — no more trades today (loss: ${abs(self.state.today_profit):.2f})",
                blocked=True,
            )
        return None

    def _check_daily_profit_target(self) -> Optional[RuleViolation]:
        """Lock in gains if today's profit exceeds $300."""
        if self._daily_profit_lockout:
            return RuleViolation(
                rule="DAILY_PROFIT_TARGET",
                message="Daily profit target hit — locking in gains",
                blocked=True,
            )
        if self.state.today_profit >= DAILY_PROFIT_TARGET:
            self._daily_profit_lockout = True
            self._lockout_date = date.today()
            logger.info(f"Daily profit target hit — locking in gains (profit: ${self.state.today_profit:.2f})")
            return RuleViolation(
                rule="DAILY_PROFIT_TARGET",
                message=f"Daily profit target hit — locking in gains (profit: ${self.state.today_profit:.2f})",
                blocked=True,
            )
        return None

    def _check_trailing_profit_protection(self) -> Optional[RuleViolation]:
        """
        If today's profit was ever $200+ and drops by $100 from peak, stop trading.
        Example: made $250, gives back $100 → now at $150 → stop trading today
        """
        if self._trailing_profit_lockout:
            return RuleViolation(
                rule="TRAILING_PROFIT_PROTECTION",
                message="Trailing profit protection triggered — preserving gains",
                blocked=True,
            )

        peak = self.state.today_peak_profit
        current = self.state.today_profit
        giveback = peak - current

        # Trigger: peak was $200+, and we've given back $100+
        if peak >= 200 and giveback >= 100:
            self._trailing_profit_lockout = True
            self._lockout_date = date.today()
            logger.warning(
                f"Trailing profit protection triggered — "
                f"peak was ${peak:.2f}, now ${current:.2f} (gave back ${giveback:.2f})"
            )
            return RuleViolation(
                rule="TRAILING_PROFIT_PROTECTION",
                message=f"Trailing profit protection: peak ${peak:.2f} → ${current:.2f} (gave back ${giveback:.2f})",
                blocked=True,
            )
        return None

    def _get_effective_max_contracts(self) -> int:
        """
        Return effective max contracts, reduced if weekly profit target hit.
        Weekly profit > $1,000 → reduce to 1 contract.
        """
        if self.state.weekly_profit >= WEEKLY_PROFIT_TARGET:
            logger.info(
                f"Weekly profit target hit (${self.state.weekly_profit:.2f}) — "
                f"reducing max contracts to 1"
            )
            return 1
        return MAX_CONTRACTS

    def _check_internal_drawdown(self, stop_pts: float, contracts: int) -> Optional[RuleViolation]:
        """Check against internal stricter drawdown limit ($1,500)."""
        max_loss_this_trade = stop_pts * MES_POINT_VALUE * contracts
        if max_loss_this_trade > self.state.internal_drawdown_remaining - 50:
            return RuleViolation(
                rule="INTERNAL_DRAWDOWN",
                message=(
                    f"Trade max loss ${max_loss_this_trade:.2f} would approach internal drawdown limit. "
                    f"Internal remaining: ${self.state.internal_drawdown_remaining:.2f}"
                ),
                blocked=True,
            )
        return None

    # ── Original MFFU rule checks ─────────────────────────

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

    def _check_contract_limit(self, contracts: int, effective_max: int) -> Optional[RuleViolation]:
        if contracts > effective_max:
            reason = ""
            if effective_max < MAX_CONTRACTS:
                reason = " (reduced due to weekly profit target)"
            return RuleViolation(
                rule="CONTRACT_LIMIT",
                message=f"Requested {contracts} contracts exceeds max {effective_max}{reason}",
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

    def _reset_daily_lockouts_if_needed(self):
        """Reset daily counters and lockouts at midnight."""
        today = date.today()
        if self.last_trade_date != today:
            self.trade_count_today = 0
            self.last_trade_date   = today

        # Reset lockouts if date changed
        if self._lockout_date and self._lockout_date != today:
            if self._daily_loss_lockout:
                logger.info("Daily loss lockout reset at midnight")
            if self._daily_profit_lockout:
                logger.info("Daily profit lockout reset at midnight")
            if self._trailing_profit_lockout:
                logger.info("Trailing profit lockout reset at midnight")
            self._daily_loss_lockout = False
            self._daily_profit_lockout = False
            self._trailing_profit_lockout = False
            self._lockout_date = None

    # ── Status summary ─────────────────────────────────────

    @property
    def is_challenge_blown(self) -> bool:
        return self._challenge_blown

    def status(self) -> dict:
        effective_max_contracts = self._get_effective_max_contracts()
        weekly_reduced = effective_max_contracts < MAX_CONTRACTS

        # Trailing profit protection info
        peak = self.state.today_peak_profit
        current = self.state.today_profit
        trailing_protection_level = peak - 100 if peak >= 200 else None

        return {
            "balance":                  self.state.current_balance,
            "high_water_mark":          self.state.high_water_mark,
            "drawdown_used":            self.state.drawdown_used,
            "drawdown_remaining":       self.state.drawdown_remaining,
            "internal_drawdown_remaining": self.state.internal_drawdown_remaining,

            # Daily metrics
            "today_profit":             self.state.today_profit,
            "today_peak_profit":        peak,
            "trailing_protection_level": trailing_protection_level,
            "daily_loss_limit":         DAILY_LOSS_LIMIT,
            "daily_profit_target":      DAILY_PROFIT_TARGET,
            "daily_loss_lockout":       self._daily_loss_lockout,
            "daily_profit_lockout":     self._daily_profit_lockout,
            "trailing_profit_lockout":  self._trailing_profit_lockout,

            # Weekly metrics
            "weekly_profit":            self.state.weekly_profit,
            "weekly_profit_target":     WEEKLY_PROFIT_TARGET,
            "weekly_contracts_reduced": weekly_reduced,
            "effective_max_contracts":  effective_max_contracts,

            # Cycle metrics
            "cycle_profit":             self.state.cycle_profit,
            "profit_target":            PROFIT_TARGET,
            "pct_to_target":            round(self.state.cycle_profit / PROFIT_TARGET * 100, 1),

            # Trades
            "trades_today":             self.trade_count_today,

            # Critical flags
            "challenge_blown":          self._challenge_blown,
            "internal_max_drawdown":    INTERNAL_MAX_DRAWDOWN,
        }

    def clear_challenge_blown(self):
        """
        Manual intervention to clear CHALLENGE_BLOWN flag.
        Only call this after reviewing the situation and updating .env.
        """
        self._challenge_blown = False
        set_bot_state("CHALLENGE_BLOWN", "False")
        logger.warning("CHALLENGE_BLOWN flag cleared by manual intervention")
