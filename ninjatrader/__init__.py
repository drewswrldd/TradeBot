"""
NinjaTrader integration for ATS Trading Agent.

Components:
    - ATSBridge.cs: NinjaScript AddOn that runs HTTP server on localhost:8080
    - bridge_client.py: Python client that replaces TradovateClient
"""

from .bridge_client import NinjaTraderBridgeClient

__all__ = ["NinjaTraderBridgeClient"]
