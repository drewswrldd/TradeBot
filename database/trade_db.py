"""
Trade Database - SQLite storage for signals and trades.
Uses SQLite for simplicity (no RDS needed).
"""

import sqlite3
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from contextlib import contextmanager

logger = logging.getLogger(__name__)

# Default database path - can be overridden via config
DEFAULT_DB_PATH = Path(__file__).parent.parent / "data" / "trades.db"


class TradeDatabase:
    """
    SQLite database for logging all signals received and trades executed.
    Thread-safe with connection-per-operation pattern.
    """

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = Path(db_path) if db_path else DEFAULT_DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_tables()
        logger.info(f"Trade database initialized at {self.db_path}")

    @contextmanager
    def _get_conn(self):
        """Context manager for thread-safe database connections."""
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_tables(self):
        """Create tables if they don't exist."""
        with self._get_conn() as conn:
            cursor = conn.cursor()

            # Signals table - every webhook received
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS signals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    direction TEXT NOT NULL,
                    trigger_high REAL NOT NULL,
                    trigger_low REAL NOT NULL,
                    atr REAL NOT NULL,
                    bar_time TEXT,
                    received_at TEXT NOT NULL,
                    confirmed INTEGER DEFAULT 0,
                    entry_price REAL,
                    rejection_reason TEXT
                )
            """)

            # Trades table - every trade opened/closed
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    signal_id INTEGER,
                    direction TEXT NOT NULL,
                    entry_price REAL NOT NULL,
                    stop_price REAL NOT NULL,
                    atr REAL NOT NULL,
                    contracts INTEGER NOT NULL,
                    target_2r REAL NOT NULL,
                    entry_time TEXT NOT NULL,
                    exit_time TEXT,
                    exit_price REAL,
                    exit_reason TEXT,
                    pnl REAL,
                    drawdown_at_entry REAL,
                    cycle_profit_at_entry REAL,
                    FOREIGN KEY (signal_id) REFERENCES signals(id)
                )
            """)

            # Daily stats view helper
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS daily_stats (
                    date TEXT PRIMARY KEY,
                    trades_count INTEGER DEFAULT 0,
                    wins INTEGER DEFAULT 0,
                    losses INTEGER DEFAULT 0,
                    total_pnl REAL DEFAULT 0,
                    max_drawdown REAL DEFAULT 0
                )
            """)

    # ── Signal Logging ─────────────────────────────────────────

    def log_signal(self, direction: str, trigger_high: float, trigger_low: float,
                   atr: float, bar_time: str = None) -> int:
        """
        Log a received webhook signal.
        Returns the signal_id for later updates.
        """
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO signals (direction, trigger_high, trigger_low, atr, bar_time, received_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                direction,
                trigger_high,
                trigger_low,
                atr,
                bar_time,
                datetime.now(timezone.utc).isoformat()
            ))
            signal_id = cursor.lastrowid
            logger.info(f"Signal logged: id={signal_id} {direction.upper()} H:{trigger_high} L:{trigger_low}")
            return signal_id

    def update_signal_confirmed(self, signal_id: int, entry_price: float):
        """Mark signal as confirmed with entry price."""
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE signals SET confirmed = 1, entry_price = ?
                WHERE id = ?
            """, (entry_price, signal_id))

    def update_signal_rejected(self, signal_id: int, reason: str):
        """Mark signal as rejected with reason."""
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE signals SET confirmed = 0, rejection_reason = ?
                WHERE id = ?
            """, (reason, signal_id))

    # ── Trade Logging ──────────────────────────────────────────

    def log_trade_open(self, signal_id: Optional[int], direction: str, entry_price: float,
                       stop_price: float, atr: float, contracts: int, target_2r: float,
                       drawdown_at_entry: float, cycle_profit_at_entry: float) -> int:
        """
        Log a newly opened trade.
        Returns the trade_id for later updates.
        """
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO trades (
                    signal_id, direction, entry_price, stop_price, atr, contracts,
                    target_2r, entry_time, drawdown_at_entry, cycle_profit_at_entry
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                signal_id,
                direction,
                entry_price,
                stop_price,
                atr,
                contracts,
                target_2r,
                datetime.now(timezone.utc).isoformat(),
                drawdown_at_entry,
                cycle_profit_at_entry
            ))
            trade_id = cursor.lastrowid
            logger.info(
                f"Trade opened: id={trade_id} {direction.upper()} @ {entry_price} "
                f"stop={stop_price} contracts={contracts}"
            )
            return trade_id

    def log_trade_close(self, trade_id: int, exit_price: float, exit_reason: str, pnl: float):
        """Log trade closure with exit details."""
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE trades
                SET exit_time = ?, exit_price = ?, exit_reason = ?, pnl = ?
                WHERE id = ?
            """, (
                datetime.now(timezone.utc).isoformat(),
                exit_price,
                exit_reason,
                pnl,
                trade_id
            ))
            logger.info(f"Trade closed: id={trade_id} @ {exit_price} reason={exit_reason} pnl=${pnl:.2f}")

            # Update daily stats
            self._update_daily_stats(conn, pnl)

    def _update_daily_stats(self, conn, pnl: float):
        """Update or insert daily stats."""
        today = datetime.now(timezone.utc).date().isoformat()
        cursor = conn.cursor()

        cursor.execute("SELECT * FROM daily_stats WHERE date = ?", (today,))
        row = cursor.fetchone()

        if row:
            cursor.execute("""
                UPDATE daily_stats
                SET trades_count = trades_count + 1,
                    wins = wins + ?,
                    losses = losses + ?,
                    total_pnl = total_pnl + ?
                WHERE date = ?
            """, (1 if pnl > 0 else 0, 1 if pnl < 0 else 0, pnl, today))
        else:
            cursor.execute("""
                INSERT INTO daily_stats (date, trades_count, wins, losses, total_pnl)
                VALUES (?, 1, ?, ?, ?)
            """, (today, 1 if pnl > 0 else 0, 1 if pnl < 0 else 0, pnl))

    # ── Queries ────────────────────────────────────────────────

    def get_todays_trades(self) -> list[dict]:
        """Get all trades from today."""
        today = datetime.now(timezone.utc).date().isoformat()
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM trades
                WHERE date(entry_time) = ?
                ORDER BY entry_time DESC
            """, (today,))
            return [dict(row) for row in cursor.fetchall()]

    def get_all_time_stats(self) -> dict:
        """Get aggregate statistics across all trades."""
        with self._get_conn() as conn:
            cursor = conn.cursor()

            # Total trades and PnL
            cursor.execute("""
                SELECT
                    COUNT(*) as total_trades,
                    SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
                    SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END) as losses,
                    SUM(CASE WHEN pnl = 0 OR pnl IS NULL THEN 1 ELSE 0 END) as breakeven,
                    COALESCE(SUM(pnl), 0) as total_pnl,
                    COALESCE(AVG(pnl), 0) as avg_pnl
                FROM trades
                WHERE exit_time IS NOT NULL
            """)
            row = cursor.fetchone()

            total_trades = row['total_trades'] or 0
            wins = row['wins'] or 0
            losses = row['losses'] or 0

            # Calculate average R (risk-reward)
            cursor.execute("""
                SELECT AVG(
                    CASE
                        WHEN pnl IS NOT NULL AND atr > 0 AND contracts > 0
                        THEN pnl / (atr * 5.0 * contracts)
                        ELSE 0
                    END
                ) as avg_r
                FROM trades
                WHERE exit_time IS NOT NULL
            """)
            avg_r_row = cursor.fetchone()
            avg_r = avg_r_row['avg_r'] if avg_r_row['avg_r'] else 0

            return {
                'total_trades': total_trades,
                'wins': wins,
                'losses': losses,
                'win_rate': round(wins / total_trades * 100, 1) if total_trades > 0 else 0,
                'total_pnl': round(row['total_pnl'] or 0, 2),
                'avg_pnl': round(row['avg_pnl'] or 0, 2),
                'avg_r': round(avg_r, 2),
            }

    def get_recent_signals(self, limit: int = 20) -> list[dict]:
        """Get recent signals."""
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM signals
                ORDER BY received_at DESC
                LIMIT ?
            """, (limit,))
            return [dict(row) for row in cursor.fetchall()]

    def get_open_trades(self) -> list[dict]:
        """Get currently open trades (no exit_time)."""
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM trades
                WHERE exit_time IS NULL
                ORDER BY entry_time DESC
            """)
            return [dict(row) for row in cursor.fetchall()]
