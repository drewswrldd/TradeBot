"""
Tradovate WebSocket client.
Subscribes to live 1H bar data for MES and streams price ticks.
Used by the bar confirmation monitor to detect entry triggers.
"""

import json
import time
import logging
import threading
import websocket
from typing import Callable, Optional
from config import TRADOVATE_WS_MD, TRADOVATE_WS_TRADING

logger = logging.getLogger(__name__)


class TradovateWebSocket:
    """
    Manages a single WebSocket connection to Tradovate.
    Calls on_bar(bar_data) when a completed or updated bar arrives.
    Calls on_tick(price) on every trade tick.
    """

    def __init__(self, access_token: str,
                 on_bar:  Callable = None,
                 on_tick: Callable = None,
                 ws_url:  str = None):
        self.access_token = access_token
        self.on_bar       = on_bar
        self.on_tick      = on_tick
        self.ws_url       = ws_url or TRADOVATE_WS_MD
        self._ws          = None
        self._thread      = None
        self._running     = False
        self._request_id  = 0
        self._subscriptions: dict[int, str] = {}   # request_id → symbol

    # ── Public API ─────────────────────────────────────────

    def connect(self):
        """Open WebSocket and authenticate."""
        self._running = True
        self._ws = websocket.WebSocketApp(
            self.ws_url,
            on_open    = self._on_open,
            on_message = self._on_message,
            on_error   = self._on_error,
            on_close   = self._on_close,
        )
        self._thread = threading.Thread(
            target=self._ws.run_forever,
            kwargs={"ping_interval": 30, "ping_timeout": 10},
            daemon=True,
        )
        self._thread.start()
        logger.info(f"WebSocket connecting to {self.ws_url}")

    def subscribe_chart(self, symbol: str, timeframe: str = "1H", contract_id: int = None):
        """
        Subscribe to bar data for a symbol.
        timeframe: '1H', '15', '5', '1D' etc.
        """
        self._request_id += 1
        rid = self._request_id
        self._subscriptions[rid] = symbol

        payload = {
            "op":           "subscribe",
            "topic":        "md/chart",
            "args": {
                "symbol":        symbol,
                "chartDescription": {
                    "underlyingType": "MinuteBar",
                    "elementSize":    60,   # 60 minutes = 1H
                    "elementSizeUnit": "UnderlyingUnits",
                    "withHistogram":  False,
                },
                "timeRange": {
                    "asMuchAsElements": 2,   # just need last 2 bars
                },
            },
        }
        self._send(payload)
        logger.info(f"Subscribed to {timeframe} bars for {symbol}")

    def subscribe_quotes(self, symbol: str):
        """Subscribe to live tick data for a symbol."""
        self._request_id += 1
        payload = {
            "op":    "subscribe",
            "topic": "md/subscribequote",
            "args":  {"symbol": symbol},
        }
        self._send(payload)
        logger.info(f"Subscribed to quotes for {symbol}")

    def disconnect(self):
        self._running = False
        if self._ws:
            self._ws.close()
        logger.info("WebSocket disconnected")

    # ── WebSocket callbacks ────────────────────────────────

    def _on_open(self, ws):
        logger.info("WebSocket connected — authenticating")
        auth_msg = f"authorize\n0\n\n{self.access_token}"
        ws.send(auth_msg)

    def _on_message(self, ws, raw: str):
        # Tradovate frames arrive as plain text with \n delimiters
        # or as a heartbeat "o", "h", "c"
        if raw in ("o", "h", "c"):
            return

        # Strip leading frame type char if present
        if raw.startswith("a"):
            raw = raw[1:]

        try:
            messages = json.loads(raw)
            if isinstance(messages, list):
                for msg in messages:
                    self._dispatch(json.loads(msg) if isinstance(msg, str) else msg)
            else:
                self._dispatch(messages)
        except json.JSONDecodeError:
            pass   # heartbeat or non-JSON frame

    def _dispatch(self, msg: dict):
        event = msg.get("e") or msg.get("event") or ""

        if event == "md/chart":
            bars = msg.get("d", {}).get("charts", [])
            for bar in bars:
                if self.on_bar:
                    self.on_bar(bar)

        elif event in ("md/quote", "md/subscribequote"):
            quotes = msg.get("d", {}).get("quotes", [])
            for q in quotes:
                price = q.get("price") or q.get("ask")
                if price and self.on_tick:
                    self.on_tick(float(price))

        elif event == "s":
            # subscription acknowledgement
            logger.debug(f"Subscription ack: {msg}")

        elif "error" in str(msg).lower():
            logger.error(f"WebSocket error frame: {msg}")

    def _on_error(self, ws, error):
        logger.error(f"WebSocket error: {error}")
        if self._running:
            time.sleep(5)
            logger.info("Attempting WebSocket reconnect...")
            self.connect()

    def _on_close(self, ws, code, reason):
        logger.warning(f"WebSocket closed: {code} {reason}")
        if self._running:
            time.sleep(5)
            logger.info("Attempting WebSocket reconnect...")
            self.connect()

    # ── Helpers ────────────────────────────────────────────

    def _send(self, payload: dict):
        if self._ws and self._ws.sock and self._ws.sock.connected:
            self._ws.send(json.dumps(payload))
        else:
            logger.warning("WebSocket not connected — cannot send")
