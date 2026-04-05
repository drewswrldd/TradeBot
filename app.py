"""
ATS Trading Agent — Main Flask App.
Receives TradingView webhook alerts and orchestrates the agent pipeline.
"""

import logging
import hmac
import hashlib
from flask import Flask, request, jsonify
from apscheduler.schedulers.background import BackgroundScheduler
import pytz

from config import (
    WEBHOOK_SECRET,
    INSTRUMENT,
    NINJATRADER_BRIDGE_URL,
)
from ninjatrader.bridge_client import NinjaTraderBridgeClient
from rules.mffu_rules import MFFURulesEngine
from rules.news_calendar import NewsCalendar
from rules.risk import calculate_position_size, round_to_tick
from monitor.bar_monitor import BarConfirmationMonitor, PendingSignal
from monitor.position_monitor import PositionMonitor, OpenTrade
from agent.agent import ATSAgent

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
bar_monitor     = None   # initialized after on_entry_confirmed is defined
position_monitor = None  # initialized after exit callbacks are defined
ws_client       = None
agent           = None


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
    logger.info(f"Handing confirmed entry to agent: {signal.direction.upper()} @ {entry_price}")
    try:
        agent.execute_entry(signal, entry_price)
    except Exception as e:
        logger.error(f"Agent entry execution failed: {e}", exc_info=True)


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
    except Exception as e:
        logger.error(f"Full exit failed: {e}", exc_info=True)


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
        "swing_extreme": 5265.00,   // swing low for longs, swing high for shorts
        "bar_time":     "2025-04-05T14:00:00Z",
        "close_price":  5278.75     // close of the bar that caused the color change
    }
    """
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
    swing_extreme = data.get("swing_extreme")
    bar_time     = data.get("bar_time", "")
    close_price  = data.get("close_price")

    logger.info(
        f"ATS webhook received: {color.upper()} | {direction.upper()} | "
        f"H:{trigger_high} L:{trigger_low} Swing:{swing_extreme}"
    )

    # ── If ATS reversed against open trade, notify position monitor ──
    if position_monitor and position_monitor.has_open_trade():
        position_monitor.on_ats_reversal(color, close_price)
        # Clear any pending entry signal on reversal
        if bar_monitor:
            bar_monitor.clear()
        return jsonify({"status": "reversal_processed"}), 200

    # ── No open trade — queue the new signal for confirmation ──
    if not all([trigger_high, trigger_low, swing_extreme]):
        return jsonify({"error": "Missing required price fields"}), 400

    signal = PendingSignal(
        direction     = direction,
        trigger_high  = float(trigger_high),
        trigger_low   = float(trigger_low),
        swing_extreme = float(swing_extreme),
        ats_bar_time  = bar_time,
    )

    if bar_monitor:
        bar_monitor.set_signal(signal)

    return jsonify({
        "status":        "signal_queued",
        "direction":     direction,
        "entry_trigger": signal.entry_trigger_price,
        "stop":          signal.stop_price,
    }), 200


# ── Status endpoint ────────────────────────────────────────

@app.route("/status", methods=["GET"])
def status():
    return jsonify({
        "rules":    rules_engine.status() if rules_engine else {},
        "bar":      bar_monitor.status()  if bar_monitor  else {},
        "position": position_monitor.status() if position_monitor else {},
        "next_event": str(news_calendar.next_event()) if news_calendar else None,
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


# ── Startup ────────────────────────────────────────────────

if __name__ == "__main__":
    bootstrap()
    app.run(host="0.0.0.0", port=5001, debug=False)
