"""
Tradovate REST API client.
Handles authentication, token refresh, and all REST calls.
"""

import time
import logging
import requests
from config import (
    TRADOVATE_BASE_URL, TRADOVATE_USERNAME, TRADOVATE_PASSWORD,
    TRADOVATE_APP_ID, TRADOVATE_APP_VERSION, TRADOVATE_CID, TRADOVATE_SEC
)

logger = logging.getLogger(__name__)


class TradovateClient:
    def __init__(self):
        self.base_url    = TRADOVATE_BASE_URL
        self.access_token = None
        self.expiration   = None
        self.account_id   = None
        self._session     = requests.Session()

    # ── Auth ───────────────────────────────────────────────

    def authenticate(self):
        """Obtain access token. Call once at startup."""
        payload = {
            "name":       TRADOVATE_USERNAME,
            "password":   TRADOVATE_PASSWORD,
            "appId":      TRADOVATE_APP_ID,
            "appVersion": TRADOVATE_APP_VERSION,
            "cid":        TRADOVATE_CID,
            "sec":        TRADOVATE_SEC,
        }
        resp = self._session.post(
            f"{self.base_url}/auth/accesstokenrequest",
            json=payload,
            timeout=10
        )
        resp.raise_for_status()
        data = resp.json()

        if "errorText" in data:
            raise RuntimeError(f"Tradovate auth failed: {data['errorText']}")

        self.access_token = data["accessToken"]
        self.expiration   = data["expirationTime"]
        self._session.headers.update({"Authorization": f"Bearer {self.access_token}"})
        logger.info("Tradovate authenticated successfully")

        # resolve account id
        self.account_id = self._resolve_account_id()
        logger.info(f"Trading account ID: {self.account_id}")
        return self.access_token

    def _resolve_account_id(self):
        """Return the first account ID associated with this login."""
        accounts = self.get("account/list")
        if not accounts:
            raise RuntimeError("No accounts found on this Tradovate login")
        return accounts[0]["id"]

    def refresh_if_needed(self):
        """Re-authenticate if token is within 5 minutes of expiry."""
        if not self.expiration:
            return
        exp_ms = int(self.expiration) if str(self.expiration).isdigit() else 0
        now_ms = int(time.time() * 1000)
        if exp_ms - now_ms < 300_000:   # 5 minutes
            logger.info("Token near expiry — refreshing")
            self.authenticate()

    # ── HTTP helpers ───────────────────────────────────────

    def get(self, path: str, params: dict = None):
        self.refresh_if_needed()
        resp = self._session.get(f"{self.base_url}/{path}", params=params, timeout=10)
        resp.raise_for_status()
        return resp.json()

    def post(self, path: str, payload: dict = None):
        self.refresh_if_needed()
        resp = self._session.post(f"{self.base_url}/{path}", json=payload or {}, timeout=10)
        resp.raise_for_status()
        return resp.json()

    # ── Account ────────────────────────────────────────────

    def get_account_summary(self) -> dict:
        """Return cash balance, realized P&L, open P&L for the account."""
        data = self.get(f"cashBalance/getcashbalancesnapshot", params={"accountId": self.account_id})
        return {
            "cash_balance":   data.get("cashBalance", 0),
            "realized_pnl":  data.get("realizedPnL", 0),
            "open_pnl":      data.get("openPnL", 0),
            "total_equity":  data.get("totalCashValue", 0),
        }

    # ── Market data ────────────────────────────────────────

    def get_quote(self, symbol: str) -> dict:
        """Get current bid/ask/last for a symbol."""
        contracts = self.get("contract/find", params={"name": symbol})
        if not contracts:
            raise ValueError(f"Symbol not found: {symbol}")
        contract_id = contracts["id"]
        quotes = self.get("quote/list", params={"contractId": contract_id})
        if not quotes:
            raise ValueError(f"No quote data for {symbol}")
        q = quotes[0]
        return {
            "symbol":      symbol,
            "contract_id": contract_id,
            "bid":         q.get("bid"),
            "ask":         q.get("ask"),
            "last":        q.get("price"),
            "timestamp":   q.get("timestamp"),
        }

    def get_contract_id(self, symbol: str) -> int:
        contract = self.get("contract/find", params={"name": symbol})
        if not contract:
            raise ValueError(f"Contract not found: {symbol}")
        return contract["id"]

    # ── Orders ─────────────────────────────────────────────

    def place_market_order(self, symbol: str, action: str, qty: int) -> dict:
        """
        Place a market order.
        action: 'Buy' | 'Sell'
        """
        contract_id = self.get_contract_id(symbol)
        payload = {
            "accountSpec":    TRADOVATE_USERNAME,
            "accountId":      self.account_id,
            "action":         action,
            "symbol":         symbol,
            "orderQty":       qty,
            "orderType":      "Market",
            "isAutomated":    True,
        }
        result = self.post("order/placeorder", payload)
        logger.info(f"Market order placed: {action} {qty} {symbol} → {result}")
        return result

    def place_bracket_order(self, symbol: str, action: str, qty: int,
                             stop_price: float, limit_price: float = None) -> dict:
        """
        Place a market entry with a stop loss bracket.
        Optionally includes a limit (take profit) leg.
        """
        contract_id = self.get_contract_id(symbol)
        stop_action = "Sell" if action == "Buy" else "Buy"

        oso_orders = [{
            "action":    stop_action,
            "orderType": "Stop",
            "stopPrice": stop_price,
            "orderQty":  qty,
        }]
        if limit_price:
            oso_orders.append({
                "action":     stop_action,
                "orderType":  "Limit",
                "price":      limit_price,
                "orderQty":   qty,
            })

        payload = {
            "accountSpec": TRADOVATE_USERNAME,
            "accountId":   self.account_id,
            "action":      action,
            "symbol":      symbol,
            "orderQty":    qty,
            "orderType":   "Market",
            "isAutomated": True,
            "bracket1":    oso_orders[0],
            "bracket2":    oso_orders[1] if limit_price else None,
        }
        # strip None values
        payload = {k: v for k, v in payload.items() if v is not None}
        result = self.post("order/placeoso", payload)
        logger.info(f"Bracket order placed: {action} {qty} {symbol} stop={stop_price} → {result}")
        return result

    def cancel_order(self, order_id: int) -> dict:
        result = self.post("order/cancelorder", {"orderId": order_id})
        logger.info(f"Order {order_id} cancelled")
        return result

    def modify_stop(self, order_id: int, new_stop: float) -> dict:
        result = self.post("order/modifyorder", {
            "orderId":   order_id,
            "orderType": "Stop",
            "stopPrice": new_stop,
        })
        logger.info(f"Stop modified: order {order_id} → {new_stop}")
        return result

    # ── Positions ──────────────────────────────────────────

    def get_positions(self) -> list:
        """Return all open positions for this account."""
        return self.get("position/list", params={"accountId": self.account_id}) or []

    def get_position(self, symbol: str) -> dict | None:
        """Return the open position for a specific symbol, or None."""
        positions = self.get_positions()
        for p in positions:
            if p.get("contractId") and self._contract_name(p["contractId"]) == symbol:
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
        qty    = abs(net_pos)
        return self.place_market_order(symbol, action, qty)

    def flatten_all(self) -> dict:
        """Emergency: close every open position immediately."""
        result = self.post("order/liquidateposition", {"accountId": self.account_id})
        logger.warning("FLATTEN ALL executed")
        return result

    # ── Orders list ────────────────────────────────────────

    def get_open_orders(self) -> list:
        return self.get("order/list", params={"accountId": self.account_id}) or []

    # ── Helpers ────────────────────────────────────────────

    def _contract_name(self, contract_id: int) -> str:
        try:
            c = self.get(f"contract/{contract_id}")
            return c.get("name", "")
        except Exception:
            return ""
