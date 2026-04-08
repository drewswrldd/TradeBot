"""
NinjaTrader Bridge Client.

Drop-in replacement for TradovateClient that sends HTTP requests to the
NinjaTrader ATSBridge AddOn (locally or via ngrok tunnel).

Usage:
    # In app.py, replace:
    #   from tradovate.client import TradovateClient
    # with:
    #   from ninjatrader.bridge_client import NinjaTraderBridgeClient as TradovateClient

Environment Variables:
    NINJATRADER_BRIDGE_URL: URL of the NinjaTrader bridge (default: http://localhost:8080)
"""

import logging
import os
import requests
from typing import Optional

logger = logging.getLogger(__name__)

# Read bridge URL from environment, default to localhost
NINJATRADER_BRIDGE_URL = os.getenv("NINJATRADER_BRIDGE_URL", "http://localhost:8080")

# Map TradingView/Tradovate symbols to NinjaTrader format
# MESH6 → MES 03-26 (March 2026) - current front-month
SYMBOL_MAP = {
    "MESH6": "MES 03-26",
    "MESM6": "MES 06-26",
    "MESU6": "MES 09-26",
    "MESZ6": "MES 12-26",
}


class NinjaTraderBridgeClient:
    """
    HTTP client for the NinjaTrader ATSBridge AddOn.

    Implements the same interface as TradovateClient for drop-in replacement.
    """

    def __init__(self, base_url: str = NINJATRADER_BRIDGE_URL):
        self.base_url = base_url.rstrip("/")
        self.access_token = "ninjatrader"  # Dummy token for compatibility
        self.account_id = None
        self._session = requests.Session()
        self._session.headers.update({"Content-Type": "application/json"})

    # ── Auth (no-op for NinjaTrader) ─────────────────────────────────────

    def authenticate(self):
        """
        Verify NinjaTrader bridge is running and get account info.
        No actual authentication needed since NinjaTrader handles that.
        """
        try:
            status = self._get_status()
            if status.get("connected"):
                self.account_id = status.get("account_name", "NT_ACCOUNT")
                logger.info(f"NinjaTrader bridge connected: {self.account_id}")
                return self.access_token
            else:
                raise RuntimeError(f"NinjaTrader bridge not connected: {status.get('error')}")
        except requests.exceptions.ConnectionError:
            raise RuntimeError(
                f"Cannot connect to NinjaTrader bridge at {self.base_url}. "
                "Ensure NinjaTrader is running with ATSBridge AddOn enabled."
            )

    def refresh_if_needed(self):
        """No token refresh needed for NinjaTrader."""
        pass

    # ── Internal HTTP helpers ────────────────────────────────────────────

    def _get_status(self) -> dict:
        """Get status from the bridge."""
        resp = self._session.get(f"{self.base_url}/status", timeout=5)
        resp.raise_for_status()
        return resp.json()

    def _get_price(self) -> dict:
        """Get live MES price from the bridge."""
        resp = self._session.get(f"{self.base_url}/price", timeout=5)
        resp.raise_for_status()
        return resp.json()

    def _post_order(self, payload: dict) -> dict:
        """Post an order to the bridge."""
        resp = self._session.post(f"{self.base_url}/order", json=payload, timeout=10)
        resp.raise_for_status()
        return resp.json()

    def _post_flatten(self) -> dict:
        """Flatten all positions via the bridge."""
        resp = self._session.post(f"{self.base_url}/flatten", timeout=10)
        resp.raise_for_status()
        return resp.json()

    def _map_symbol(self, symbol: str) -> str:
        """Convert Tradovate symbol to NinjaTrader format."""
        return SYMBOL_MAP.get(symbol, symbol)

    # ── Account ──────────────────────────────────────────────────────────

    def get_account_summary(self) -> dict:
        """Return cash balance, realized P&L, open P&L for the account."""
        status = self._get_status()
        return {
            "cash_balance": status.get("cash_balance", 0),
            "realized_pnl": status.get("realized_pnl", 0),
            "open_pnl": status.get("unrealized_pnl", 0),
            "total_equity": status.get("total_equity", 0),
        }

    # ── Market data ──────────────────────────────────────────────────────

    def get_quote(self, symbol: str) -> dict:
        """
        Get current quote — NinjaTrader bridge doesn't provide quotes,
        so this returns placeholder data. Use the WebSocket for real quotes.
        """
        logger.warning("get_quote not implemented in NinjaTrader bridge — use WebSocket")
        return {
            "symbol": symbol,
            "contract_id": 0,
            "bid": None,
            "ask": None,
            "last": None,
            "timestamp": None,
        }

    def get_contract_id(self, symbol: str) -> int:
        """Not applicable for NinjaTrader — return 0."""
        return 0

    # ── Orders ───────────────────────────────────────────────────────────

    def place_market_order(self, symbol: str, action: str, qty: int) -> dict:
        """
        Place a market order.

        Args:
            symbol: Tradovate symbol (e.g., MESH5)
            action: 'Buy' | 'Sell'
            qty: Number of contracts
        """
        nt_symbol = self._map_symbol(symbol)
        payload = {
            "action": action.upper(),
            "instrument": nt_symbol,
            "quantity": qty,
            "order_type": "market",
        }
        result = self._post_order(payload)

        if result.get("success"):
            logger.info(f"Market order placed: {action} {qty} {symbol} ({nt_symbol})")
        else:
            logger.error(f"Market order failed: {result.get('error')}")

        return result

    def place_bracket_order(
        self,
        symbol: str,
        action: str,
        qty: int,
        stop_price: float,
        limit_price: float = None,
    ) -> dict:
        """
        Place a market entry with a stop loss bracket.

        For NinjaTrader, we submit the entry as market, then submit a
        separate stop order. NinjaTrader's native bracket/OCO can also be used.
        """
        nt_symbol = self._map_symbol(symbol)

        # Place the entry order
        entry_payload = {
            "action": action.upper(),
            "instrument": nt_symbol,
            "quantity": qty,
            "order_type": "market",
        }
        entry_result = self._post_order(entry_payload)

        if not entry_result.get("success"):
            logger.error(f"Bracket entry failed: {entry_result.get('error')}")
            return entry_result

        # Place the stop order
        stop_action = "SELL" if action.upper() == "BUY" else "BUY"
        stop_payload = {
            "action": stop_action,
            "instrument": nt_symbol,
            "quantity": qty,
            "order_type": "stop",
            "stop_price": stop_price,
        }
        stop_result = self._post_order(stop_payload)

        if not stop_result.get("success"):
            logger.error(f"Stop order failed: {stop_result.get('error')}")

        # Optionally place limit (take profit) order
        limit_result = None
        if limit_price:
            limit_payload = {
                "action": stop_action,
                "instrument": nt_symbol,
                "quantity": qty,
                "order_type": "limit",
                "stop_price": limit_price,  # Bridge uses stop_price field for limit price too
            }
            limit_result = self._post_order(limit_payload)

        logger.info(
            f"Bracket order placed: {action} {qty} {symbol} stop={stop_price}"
            + (f" limit={limit_price}" if limit_price else "")
        )

        return {
            "entry": entry_result,
            "stop": stop_result,
            "limit": limit_result,
        }

    def cancel_order(self, order_id: int) -> dict:
        """
        Cancel an order by ID.

        Note: NinjaTrader bridge doesn't currently support individual order
        cancellation. Use flatten_all() to cancel all orders.
        """
        logger.warning("cancel_order not fully implemented — use flatten_all()")
        return {"warning": "Individual order cancellation not implemented"}

    def modify_stop(self, order_id: int, new_stop: float) -> dict:
        """
        Modify a stop order.

        Note: NinjaTrader bridge doesn't currently support order modification.
        Cancel and re-submit instead.
        """
        logger.warning("modify_stop not implemented — cancel and resubmit")
        return {"warning": "Order modification not implemented"}

    # ── Positions ────────────────────────────────────────────────────────

    def get_positions(self) -> list:
        """Return all open positions for this account."""
        status = self._get_status()
        positions = status.get("positions", [])

        # Convert to Tradovate-like format
        result = []
        for pos in positions:
            net_pos = pos.get("quantity", 0)
            if pos.get("direction") == "Short":
                net_pos = -net_pos

            result.append({
                "netPos": net_pos,
                "instrument": pos.get("instrument"),
                "averagePrice": pos.get("avg_price"),
                "unrealizedPnL": pos.get("unrealized_pnl"),
            })

        return result

    def get_position(self, symbol: str) -> Optional[dict]:
        """Return the open position for a specific symbol, or None."""
        nt_symbol = self._map_symbol(symbol)
        positions = self.get_positions()

        for p in positions:
            # Check both original and mapped symbol
            if p.get("instrument") == nt_symbol or p.get("instrument") == symbol:
                return p

        return None

    def close_position(self, symbol: str) -> dict:
        """Flatten the entire position for a symbol at market."""
        position = self.get_position(symbol)
        if not position:
            logger.warning(f"No open position found for {symbol}")
            return {}

        net_pos = position.get("netPos", 0)
        if net_pos == 0:
            return {}

        action = "Sell" if net_pos > 0 else "Buy"
        qty = abs(net_pos)
        return self.place_market_order(symbol, action, qty)

    def flatten_all(self) -> dict:
        """Emergency: close every open position and cancel all orders."""
        result = self._post_flatten()
        logger.warning("FLATTEN ALL executed via NinjaTrader bridge")
        return result

    # ── Orders list ──────────────────────────────────────────────────────

    def get_open_orders(self) -> list:
        """Return all open orders."""
        status = self._get_status()
        return status.get("open_orders", [])

    # ── Compatibility aliases ────────────────────────────────────────────

    def get(self, path: str, params: dict = None):
        """Compatibility shim — not used for NinjaTrader."""
        raise NotImplementedError("Direct REST calls not supported via NinjaTrader bridge")

    def post(self, path: str, payload: dict = None):
        """Compatibility shim — not used for NinjaTrader."""
        raise NotImplementedError("Direct REST calls not supported via NinjaTrader bridge")
