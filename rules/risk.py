"""
Risk Engine.
Calculates position size in MES contracts given a stop distance,
enforcing the ATS 2% account risk rule and the MFFU contract cap.
"""

import math
import logging
from config import ACCOUNT_SIZE, RISK_PERCENT, MAX_CONTRACTS, MES_TICK_SIZE, MES_TICK_VALUE, MES_POINT_VALUE

logger = logging.getLogger(__name__)

# Global ATR value updated by /atr-update endpoint
# Used as the authoritative ATR for position sizing when available
_global_atr = 0.0


def set_global_atr(atr: float):
    """Set the global ATR value from the /atr-update endpoint."""
    global _global_atr
    _global_atr = atr
    logger.debug(f"Global ATR set to: {atr}")


def get_global_atr() -> float:
    """Get the current global ATR value."""
    return _global_atr


def calculate_position_size(entry_price: float,
                             stop_price:  float,
                             account_balance: float = ACCOUNT_SIZE) -> dict:
    """
    Given an entry and stop price, return the number of MES contracts
    to trade such that the max loss is ≤ RISK_PERCENT of account_balance.

    Returns a dict with full sizing breakdown for the agent to log.
    """
    stop_distance_points = abs(entry_price - stop_price)

    if stop_distance_points < MES_TICK_SIZE:
        raise ValueError(f"Stop distance {stop_distance_points} is less than 1 tick")

    dollar_risk_allowed  = account_balance * (RISK_PERCENT / 100)
    dollar_per_contract  = stop_distance_points * MES_POINT_VALUE
    raw_contracts        = dollar_risk_allowed / dollar_per_contract
    contracts            = max(1, min(math.floor(raw_contracts), MAX_CONTRACTS))
    actual_dollar_risk   = contracts * dollar_per_contract

    result = {
        "entry_price":          entry_price,
        "stop_price":           stop_price,
        "stop_distance_points": round(stop_distance_points, 2),
        "stop_distance_ticks":  round(stop_distance_points / MES_TICK_SIZE, 1),
        "account_balance":      account_balance,
        "dollar_risk_allowed":  round(dollar_risk_allowed, 2),
        "dollar_per_contract":  round(dollar_per_contract, 2),
        "contracts":            contracts,
        "actual_dollar_risk":   round(actual_dollar_risk, 2),
        "risk_pct_actual":      round(actual_dollar_risk / account_balance * 100, 2),
    }

    logger.info(
        f"Position sizing: {contracts} MES | "
        f"Stop: {stop_distance_points:.2f} pts | "
        f"Risk: ${actual_dollar_risk:.2f} ({result['risk_pct_actual']}%)"
    )
    return result


def calculate_targets(entry_price: float,
                       stop_price:  float,
                       direction:   str) -> dict:
    """
    Calculate Strategy 1 price targets:
    - 2R target (50% exit)
    - ATS reversal handles the remaining 50%

    direction: 'long' | 'short'
    """
    stop_distance = abs(entry_price - stop_price)
    r = stop_distance   # 1R = 1× the stop distance

    if direction == "long":
        target_2r = round(entry_price + (2 * r), 2)
    else:
        target_2r = round(entry_price - (2 * r), 2)

    return {
        "entry":     entry_price,
        "stop":      stop_price,
        "r_value":   round(r, 2),
        "target_2r": target_2r,
        "direction": direction,
    }


def round_to_tick(price: float) -> float:
    """Round a price to the nearest MES tick (0.25)."""
    return round(round(price / MES_TICK_SIZE) * MES_TICK_SIZE, 4)


def calculate_atr_stop(entry_price: float, atr: float, direction: str,
                        multiplier: float = 1.5) -> float:
    """
    Calculate stop price based on ATR.

    Uses the global ATR from /atr-update endpoint if available,
    otherwise falls back to the provided atr parameter, then to 10.0.

    Args:
        entry_price: The entry price
        atr: Average True Range value (fallback if global ATR not set)
        direction: 'long' or 'short'
        multiplier: ATR multiplier (default 1.5)

    Returns:
        Stop price rounded to nearest MES tick (0.25)
    """
    # Use global ATR if available, otherwise fallback to provided atr, then 10.0
    effective_atr = _global_atr if _global_atr > 0 else (atr if atr > 0 else 10.0)
    atr_source = "global" if _global_atr > 0 else ("signal" if atr > 0 else "default")

    stop_distance = effective_atr * multiplier

    if direction == "long":
        stop_price = entry_price - stop_distance
    else:  # short
        stop_price = entry_price + stop_distance

    stop_price = round_to_tick(stop_price)

    logger.info(
        f"ATR stop calculated: {direction.upper()} | "
        f"Entry: {entry_price} | ATR: {effective_atr} ({atr_source}) | "
        f"Stop distance: {stop_distance:.2f} ({multiplier}x ATR) | "
        f"Stop: {stop_price}"
    )

    return stop_price
