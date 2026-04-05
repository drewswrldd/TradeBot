"""
ATS Trading Agent — Main Flask App.
Receives TradingView webhook alerts and orchestrates the agent pipeline.
"""

import logging
from flask import Flask, request, jsonify, render_template_string
from apscheduler.schedulers.background import BackgroundScheduler
import pytz
from datetime import datetime, timezone

from config import (
    WEBHOOK_SECRET,
    INSTRUMENT,
    NINJATRADER_BRIDGE_URL,
    PROFIT_TARGET,
    MAX_DRAWDOWN,
    ACCOUNT_SIZE,
)
from ninjatrader.bridge_client import NinjaTraderBridgeClient
from rules.mffu_rules import MFFURulesEngine
from rules.news_calendar import NewsCalendar
from rules.risk import calculate_position_size, round_to_tick
from monitor.bar_monitor import BarConfirmationMonitor, PendingSignal
from monitor.position_monitor import PositionMonitor, OpenTrade
from agent.agent import ATSAgent
from database.trade_db import TradeDatabase

# ── Logging ────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("logs/agent.log"),
    ]
)
logger = logging.getLogger(__name__)

# ── Flask app ──────────────────────────────────────────────
app = Flask(__name__)

# ── Component initialization ───────────────────────────────
tv_client       = NinjaTraderBridgeClient()
news_calendar   = NewsCalendar()
rules_engine    = MFFURulesEngine(news_calendar=news_calendar)
trade_db        = TradeDatabase()   # SQLite trade logging
bar_monitor     = None   # initialized after on_entry_confirmed is defined
position_monitor = None  # initialized after exit callbacks are defined
ws_client       = None
agent           = None

# Track current signal_id for linking signals to trades
_current_signal_id = None


def bootstrap():
    """Initialize all components at startup."""
    global bar_monitor, position_monitor, ws_client, agent

    logger.info("=== ATS Trading Agent starting up ===")

    # Authenticate with NinjaTrader bridge (only if URL is configured)
    if NINJATRADER_BRIDGE_URL:
        try:
            tv_client.authenticate()

            # Sync initial account state
            summary = tv_client.get_account_summary()
            rules_engine.sync_from_tradovate(summary)
            logger.info(f"Account state synced: {rules_engine.status()}")
        except Exception as e:
            logger.warning(f"NinjaTrader bridge connection failed: {e}")
    else:
        logger.warning(
            "NinjaTrader bridge not configured — skipping authentication. "
            "Set NINJATRADER_BRIDGE_URL to enable trading."
        )

    # Refresh news calendar
    news_calendar.refresh()

    # Wire up monitors with callbacks
    bar_monitor = BarConfirmationMonitor(
        on_entry_confirmed=handle_entry_confirmed
    )
    position_monitor = PositionMonitor(
        on_partial_exit=handle_partial_exit,
        on_full_exit=handle_full_exit,
        rules_engine=rules_engine,
    )

    # Initialize AI agent
    agent = ATSAgent(
        tradovate_client=tv_client,
        rules_engine=rules_engine,
        position_monitor=position_monitor,
    )

    # Note: WebSocket for live price data not available with NinjaTrader bridge
    # Price data comes from NinjaTrader directly via the bridge's /status endpoint

    # Scheduler: refresh news calendar daily, sync account periodically
    scheduler = BackgroundScheduler(timezone=pytz.utc)
    scheduler.add_job(news_calendar.refresh,             "cron", hour=0, minute=5)
    if NINJATRADER_BRIDGE_URL:
        scheduler.add_job(_sync_account,                 "interval", minutes=15)
    scheduler.start()

    logger.info("=== ATS Trading Agent ready ===")


# ── WebSocket callbacks ────────────────────────────────────

def _on_tick(price: float):
    """Distributed to bar monitor and position monitor on every tick."""
    if bar_monitor:
        bar_monitor.on_tick(price)
    if position_monitor:
        position_monitor.on_tick(price)


def _on_bar(bar: dict):
    """Received a completed 1H bar — currently used for logging."""
    logger.debug(f"Bar update: {bar}")


def _sync_account():
    try:
        summary = tv_client.get_account_summary()
        rules_engine.sync_from_tradovate(summary)
    except Exception as e:
        logger.error(f"Account sync failed: {e}")


# ── Entry confirmed callback ───────────────────────────────

def handle_entry_confirmed(signal: PendingSignal, entry_price: float):
    """
    Called by bar_monitor when price crosses the trigger bar's high/low.
    Agent takes over from here to size, validate, and place the order.
    """
    global _current_signal_id

    logger.info(f"Handing confirmed entry to agent: {signal.direction.upper()} @ {entry_price}")

    # Update signal as confirmed in database
    if _current_signal_id:
        trade_db.update_signal_confirmed(_current_signal_id, entry_price)

    try:
        # Agent will call back to log_trade_opened when order is filled
        agent.execute_entry(signal, entry_price, on_trade_opened=log_trade_opened)
    except Exception as e:
        logger.error(f"Agent entry execution failed: {e}", exc_info=True)
        if _current_signal_id:
            trade_db.update_signal_rejected(_current_signal_id, str(e))


def log_trade_opened(direction: str, entry_price: float, stop_price: float,
                     atr: float, contracts: int, target_2r: float):
    """Called by agent when a trade is successfully opened."""
    global _current_signal_id

    trade_id = trade_db.log_trade_open(
        signal_id=_current_signal_id,
        direction=direction,
        entry_price=entry_price,
        stop_price=stop_price,
        atr=atr,
        contracts=contracts,
        target_2r=target_2r,
        drawdown_at_entry=rules_engine.state.drawdown_used,
        cycle_profit_at_entry=rules_engine.state.cycle_profit,
    )

    # Store trade_id in position_monitor for later closure logging
    if position_monitor and position_monitor.trade:
        position_monitor.trade.db_trade_id = trade_id


# ── Exit callbacks ─────────────────────────────────────────

def handle_partial_exit(trade: OpenTrade, price: float):
    """50% exit at 2R."""
    try:
        action = "Sell" if trade.direction == "long" else "Buy"
        tv_client.place_market_order(INSTRUMENT, action, trade.contracts_for_partial)
        logger.info(f"Partial exit: {trade.contracts_for_partial} contracts @ {price:.2f}")
    except Exception as e:
        logger.error(f"Partial exit failed: {e}", exc_info=True)


def handle_full_exit(trade: OpenTrade, price: float, reason: str):
    """Full exit — remaining contracts."""
    try:
        action = "Sell" if trade.direction == "long" else "Buy"
        tv_client.place_market_order(INSTRUMENT, action, trade.remaining_contracts)
        logger.info(f"Full exit ({reason}): {trade.remaining_contracts} contracts @ {price:.2f}")

        # Calculate PnL and log to database
        if hasattr(trade, 'db_trade_id') and trade.db_trade_id:
            pnl = calculate_trade_pnl(trade, price)
            trade_db.log_trade_close(trade.db_trade_id, price, reason, pnl)
    except Exception as e:
        logger.error(f"Full exit failed: {e}", exc_info=True)


def calculate_trade_pnl(trade: OpenTrade, exit_price: float) -> float:
    """Calculate realized PnL for a trade."""
    from config import MES_POINT_VALUE
    if trade.direction == "long":
        points = exit_price - trade.entry_price
    else:
        points = trade.entry_price - exit_price
    return points * MES_POINT_VALUE * trade.total_contracts


# ── Webhook endpoint ───────────────────────────────────────

@app.route("/webhook/ats", methods=["POST"])
def ats_webhook():
    """
    TradingView sends POST here when ATS changes color.

    Expected JSON payload from Pine Script alert:
    {
        "secret":       "your_secret_here",
        "direction":    "long" | "short",
        "color":        "blue" | "red",
        "trigger_high": 5284.50,
        "trigger_low":  5271.25,
        "atr":          12.50,      // ATR value for stop calculation
        "bar_time":     "2025-04-05T14:00:00Z",
        "close_price":  5278.75     // close of the bar that caused the color change
    }
    """
    global _current_signal_id

    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Invalid JSON"}), 400

    # ── Verify webhook secret ──
    if data.get("secret") != WEBHOOK_SECRET:
        logger.warning("Webhook received with invalid secret")
        return jsonify({"error": "Unauthorized"}), 401

    direction    = data.get("direction", "").lower()
    color        = data.get("color", "").lower()
    trigger_high = data.get("trigger_high")
    trigger_low  = data.get("trigger_low")
    atr          = data.get("atr")
    bar_time     = data.get("bar_time", "")
    close_price  = data.get("close_price")

    logger.info(
        f"ATS webhook received: {color.upper()} | {direction.upper()} | "
        f"H:{trigger_high} L:{trigger_low} ATR:{atr}"
    )

    # ── If ATS reversed against open trade, notify position monitor ──
    if position_monitor and position_monitor.has_open_trade():
        position_monitor.on_ats_reversal(color, close_price)
        # Clear any pending entry signal on reversal
        if bar_monitor:
            bar_monitor.clear()
        return jsonify({"status": "reversal_processed"}), 200

    # ── No open trade — queue the new signal for confirmation ──
    if not all([trigger_high, trigger_low, atr]):
        return jsonify({"error": "Missing required price fields (trigger_high, trigger_low, atr)"}), 400

    # Log signal to database
    _current_signal_id = trade_db.log_signal(
        direction=direction,
        trigger_high=float(trigger_high),
        trigger_low=float(trigger_low),
        atr=float(atr),
        bar_time=bar_time,
    )

    signal = PendingSignal(
        direction     = direction,
        trigger_high  = float(trigger_high),
        trigger_low   = float(trigger_low),
        atr           = float(atr),
        ats_bar_time  = bar_time,
    )

    if bar_monitor:
        bar_monitor.set_signal(signal)

    return jsonify({
        "status":        "signal_queued",
        "signal_id":     _current_signal_id,
        "direction":     direction,
        "entry_trigger": signal.entry_trigger_price,
        "atr":           signal.atr,
    }), 200


# ── Status endpoint ────────────────────────────────────────

@app.route("/status", methods=["GET"])
def status():
    return jsonify({
        "rules":    rules_engine.status() if rules_engine else {},
        "bar":      bar_monitor.status()  if bar_monitor  else {},
        "position": position_monitor.status() if position_monitor else {},
        "next_event": str(news_calendar.next_event()) if news_calendar else None,
        "news_calendar": news_calendar.status() if news_calendar else {},
    })


@app.route("/flatten", methods=["POST"])
def manual_flatten():
    """Emergency manual flatten endpoint."""
    try:
        result = tv_client.flatten_all()
        if position_monitor:
            position_monitor.close_trade("manual_flatten")
        if bar_monitor:
            bar_monitor.clear()
        logger.warning("MANUAL FLATTEN executed via API")
        return jsonify({"status": "flattened", "result": result})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Performance Dashboard ──────────────────────────────────

DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>ATS Trading Dashboard</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, sans-serif;
            background: #0d1117;
            color: #c9d1d9;
            padding: 20px;
            line-height: 1.6;
        }
        .container { max-width: 1200px; margin: 0 auto; }
        h1 {
            color: #58a6ff;
            margin-bottom: 20px;
            font-size: 24px;
            border-bottom: 1px solid #30363d;
            padding-bottom: 10px;
        }
        h2 {
            color: #8b949e;
            font-size: 14px;
            text-transform: uppercase;
            letter-spacing: 1px;
            margin-bottom: 15px;
        }
        .grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
            gap: 20px;
            margin-bottom: 30px;
        }
        .card {
            background: #161b22;
            border: 1px solid #30363d;
            border-radius: 8px;
            padding: 20px;
        }
        .stat-row {
            display: flex;
            justify-content: space-between;
            padding: 8px 0;
            border-bottom: 1px solid #21262d;
        }
        .stat-row:last-child { border-bottom: none; }
        .stat-label { color: #8b949e; }
        .stat-value { font-weight: 600; font-family: 'SF Mono', monospace; }
        .stat-value.positive { color: #3fb950; }
        .stat-value.negative { color: #f85149; }
        .stat-value.warning { color: #d29922; }
        .progress-bar {
            background: #21262d;
            border-radius: 4px;
            height: 8px;
            margin-top: 10px;
            overflow: hidden;
        }
        .progress-fill {
            height: 100%;
            border-radius: 4px;
            transition: width 0.3s ease;
        }
        .progress-fill.green { background: linear-gradient(90deg, #238636, #3fb950); }
        .progress-fill.red { background: linear-gradient(90deg, #da3633, #f85149); }
        .progress-fill.yellow { background: linear-gradient(90deg, #9e6a03, #d29922); }
        table {
            width: 100%;
            border-collapse: collapse;
            font-size: 14px;
        }
        th, td {
            text-align: left;
            padding: 10px 12px;
            border-bottom: 1px solid #21262d;
        }
        th {
            color: #8b949e;
            font-weight: 500;
            font-size: 12px;
            text-transform: uppercase;
        }
        .badge {
            display: inline-block;
            padding: 2px 8px;
            border-radius: 12px;
            font-size: 12px;
            font-weight: 500;
        }
        .badge.long { background: #238636; color: #fff; }
        .badge.short { background: #da3633; color: #fff; }
        .badge.open { background: #1f6feb; color: #fff; }
        .refresh-note {
            text-align: center;
            color: #484f58;
            font-size: 12px;
            margin-top: 20px;
        }
        .event-list {
            font-size: 13px;
        }
        .event-item {
            padding: 8px 0;
            border-bottom: 1px solid #21262d;
        }
        .event-time { color: #58a6ff; font-family: 'SF Mono', monospace; }
        .no-data { color: #484f58; font-style: italic; }
    </style>
</head>
<body>
    <div class="container">
        <h1>ATS Trading Dashboard</h1>

        <div class="grid">
            <!-- Account Status -->
            <div class="card">
                <h2>Account Status</h2>
                <div class="stat-row">
                    <span class="stat-label">Balance</span>
                    <span class="stat-value">${{ "{:,.2f}".format(account.balance) }}</span>
                </div>
                <div class="stat-row">
                    <span class="stat-label">Cycle Profit</span>
                    <span class="stat-value {{ 'positive' if account.cycle_profit >= 0 else 'negative' }}">
                        ${{ "{:,.2f}".format(account.cycle_profit) }}
                    </span>
                </div>
                <div class="stat-row">
                    <span class="stat-label">Progress to Target</span>
                    <span class="stat-value">{{ account.pct_to_target }}%</span>
                </div>
                <div class="progress-bar">
                    <div class="progress-fill green" style="width: {{ min(account.pct_to_target, 100) }}%"></div>
                </div>
            </div>

            <!-- Risk Status -->
            <div class="card">
                <h2>Risk Status</h2>
                <div class="stat-row">
                    <span class="stat-label">Drawdown Used</span>
                    <span class="stat-value {{ 'negative' if account.drawdown_used > 1000 else 'warning' if account.drawdown_used > 500 else '' }}">
                        ${{ "{:,.2f}".format(account.drawdown_used) }}
                    </span>
                </div>
                <div class="stat-row">
                    <span class="stat-label">Drawdown Remaining</span>
                    <span class="stat-value">${{ "{:,.2f}".format(account.drawdown_remaining) }}</span>
                </div>
                <div class="stat-row">
                    <span class="stat-label">Today's Profit</span>
                    <span class="stat-value {{ 'positive' if account.today_profit >= 0 else 'negative' }}">
                        ${{ "{:,.2f}".format(account.today_profit) }}
                    </span>
                </div>
                <div class="progress-bar">
                    <div class="progress-fill {{ 'red' if account.drawdown_pct > 50 else 'yellow' if account.drawdown_pct > 25 else 'green' }}"
                         style="width: {{ account.drawdown_pct }}%"></div>
                </div>
            </div>

            <!-- All-Time Stats -->
            <div class="card">
                <h2>All-Time Performance</h2>
                <div class="stat-row">
                    <span class="stat-label">Total Trades</span>
                    <span class="stat-value">{{ stats.total_trades }}</span>
                </div>
                <div class="stat-row">
                    <span class="stat-label">Win Rate</span>
                    <span class="stat-value {{ 'positive' if stats.win_rate >= 50 else 'negative' }}">
                        {{ stats.win_rate }}%
                    </span>
                </div>
                <div class="stat-row">
                    <span class="stat-label">Avg R</span>
                    <span class="stat-value {{ 'positive' if stats.avg_r >= 0 else 'negative' }}">
                        {{ stats.avg_r }}R
                    </span>
                </div>
                <div class="stat-row">
                    <span class="stat-label">Total PnL</span>
                    <span class="stat-value {{ 'positive' if stats.total_pnl >= 0 else 'negative' }}">
                        ${{ "{:,.2f}".format(stats.total_pnl) }}
                    </span>
                </div>
            </div>

            <!-- News Events -->
            <div class="card">
                <h2>Upcoming News Events</h2>
                <div class="event-list">
                    {% if news_events %}
                        {% for event in news_events[:5] %}
                        <div class="event-item">
                            <span class="event-time">{{ event.time.strftime('%m/%d %H:%M') }} UTC</span>
                            <span> — {{ event.name }}</span>
                        </div>
                        {% endfor %}
                    {% else %}
                        <p class="no-data">No upcoming events</p>
                    {% endif %}
                </div>
            </div>
        </div>

        <!-- Today's Trades -->
        <div class="card">
            <h2>Today's Trades</h2>
            {% if todays_trades %}
            <table>
                <thead>
                    <tr>
                        <th>Direction</th>
                        <th>Entry</th>
                        <th>Stop</th>
                        <th>Exit</th>
                        <th>Reason</th>
                        <th>PnL</th>
                        <th>Status</th>
                    </tr>
                </thead>
                <tbody>
                    {% for trade in todays_trades %}
                    <tr>
                        <td><span class="badge {{ trade.direction }}">{{ trade.direction|upper }}</span></td>
                        <td>{{ "{:.2f}".format(trade.entry_price) }}</td>
                        <td>{{ "{:.2f}".format(trade.stop_price) }}</td>
                        <td>{{ "{:.2f}".format(trade.exit_price) if trade.exit_price else '—' }}</td>
                        <td>{{ trade.exit_reason or '—' }}</td>
                        <td class="{{ 'positive' if trade.pnl and trade.pnl > 0 else 'negative' if trade.pnl and trade.pnl < 0 else '' }}">
                            {{ "${:,.2f}".format(trade.pnl) if trade.pnl else '—' }}
                        </td>
                        <td>
                            {% if not trade.exit_time %}
                                <span class="badge open">OPEN</span>
                            {% else %}
                                Closed
                            {% endif %}
                        </td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
            {% else %}
                <p class="no-data">No trades today</p>
            {% endif %}
        </div>

        <p class="refresh-note">
            Last updated: {{ now.strftime('%Y-%m-%d %H:%M:%S') }} UTC
            &nbsp;|&nbsp;
            <a href="/dashboard" style="color: #58a6ff;">Refresh</a>
        </p>
    </div>
</body>
</html>
"""


@app.route("/dashboard", methods=["GET"])
def dashboard():
    """Performance dashboard with account status, trades, and stats."""
    # Get account status from rules engine
    account_status = rules_engine.status() if rules_engine else {}
    account_data = {
        'balance': account_status.get('balance', ACCOUNT_SIZE),
        'cycle_profit': account_status.get('cycle_profit', 0),
        'pct_to_target': account_status.get('pct_to_target', 0),
        'drawdown_used': account_status.get('drawdown_used', 0),
        'drawdown_remaining': account_status.get('drawdown_remaining', MAX_DRAWDOWN),
        'today_profit': account_status.get('today_profit', 0),
        'drawdown_pct': round(account_status.get('drawdown_used', 0) / MAX_DRAWDOWN * 100, 1),
    }

    # Get trade stats from database
    all_time_stats = trade_db.get_all_time_stats()
    todays_trades = trade_db.get_todays_trades()

    # Get upcoming news events
    news_events = []
    if news_calendar:
        next_event = news_calendar.next_event()
        if next_event:
            news_events = [next_event]
        # Get more events if available
        now = datetime.now(timezone.utc)
        future_events = [e for e in news_calendar._events if e['time'] > now]
        news_events = sorted(future_events, key=lambda x: x['time'])[:5]

    return render_template_string(
        DASHBOARD_HTML,
        account=account_data,
        stats=all_time_stats,
        todays_trades=todays_trades,
        news_events=news_events,
        now=datetime.now(timezone.utc),
    )


# ── Startup ────────────────────────────────────────────────

if __name__ == "__main__":
    bootstrap()
    app.run(host="0.0.0.0", port=5001, debug=False)
