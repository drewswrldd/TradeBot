"""
ATS AI Agent.
Claude-powered decision layer. Called when entry is confirmed.
Validates the trade through the rules engine, sizes the position,
and places orders via the Tradovate client.
"""

import json
import logging
import anthropic
from config import ANTHROPIC_API_KEY, INSTRUMENT, MES_POINT_VALUE
from rules.risk import calculate_position_size, calculate_targets, round_to_tick, calculate_atr_stop
from monitor.bar_monitor import PendingSignal
from monitor.position_monitor import OpenTrade

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """
You are an autonomous futures trading agent executing the AutoTrend System (ATS) Strategy 1
on MES (Micro E-mini S&P 500) for a MyFundedFutures 50K evaluation account.

Your job when called:
1. Review the confirmed entry signal and sizing data provided
2. Verify the trade makes sense (risk/reward, market context)
3. Call place_entry_order to execute, OR call reject_trade with a reason if something is wrong
4. Log your reasoning clearly

Strategy 1 rules you enforce:
- Entry: ATS color change confirmed when next bar overtakes trigger bar high (long) / low (short)
- Stop: 1.5 × ATR from entry price (below for longs, above for shorts)
- Exit 1: close 50% of position at 2R profit
- Exit 2: close remaining 50% when ATS turns the opposite color

MFFU rules you must never violate:
- Max trailing drawdown: $2,000 from high-water mark (EOD)
- Max contracts: 3 MES
- Consistency: no single day > 50% of profit target
- No trading during Tier 1 news blackout windows
- Risk no more than 2% of account per trade

Always be conservative. If anything looks off — news imminent, stop too tight, 
drawdown margin thin — reject the trade. Capital preservation first.
"""


class ATSAgent:
    def __init__(self, tradovate_client, rules_engine, position_monitor):
        self.client           = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        self.tradovate        = tradovate_client
        self.rules_engine     = rules_engine
        self.position_monitor = position_monitor

        self._tools = [
            {
                "name": "place_entry_order",
                "description": "Place the entry market order with stop loss for the confirmed ATS signal.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "action":     {"type": "string", "enum": ["Buy", "Sell"]},
                        "contracts":  {"type": "integer"},
                        "stop_price": {"type": "number"},
                        "reasoning":  {"type": "string"},
                    },
                    "required": ["action", "contracts", "stop_price", "reasoning"],
                },
            },
            {
                "name": "reject_trade",
                "description": "Reject this trade and do not place an order.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "reason": {"type": "string"},
                    },
                    "required": ["reason"],
                },
            },
        ]

    # ── Main entry point ───────────────────────────────────

    def execute_entry(self, signal: PendingSignal, entry_price: float, on_trade_opened=None):
        """
        Called when bar confirmation fires.
        Runs the agent decision loop and executes or rejects the trade.

        Args:
            signal: The pending signal that was confirmed
            entry_price: The price at which entry is triggered
            on_trade_opened: Optional callback(direction, entry_price, stop_price, atr, contracts, target_2r)
                            Called when a trade is successfully opened.
        """
        self._on_trade_opened = on_trade_opened
        # Calculate ATR-based stop price
        stop_price = calculate_atr_stop(
            entry_price = entry_price,
            atr         = signal.atr,
            direction   = signal.direction,
            multiplier  = 1.5,
        )

        # Pre-calculate sizing for the agent to reason over
        sizing = calculate_position_size(
            entry_price    = entry_price,
            stop_price     = stop_price,
            account_balance = self.rules_engine.state.current_balance,
        )
        targets = calculate_targets(entry_price, stop_price, signal.direction)
        rules_status = self.rules_engine.status()

        # Pre-check the rules engine before even calling the agent
        approved, violations = self.rules_engine.check_pre_trade(
            proposed_contracts  = sizing["contracts"],
            stop_distance_points = sizing["stop_distance_points"],
            action              = signal.direction,
        )
        if not approved:
            reasons = "; ".join(v.message for v in violations if v.blocked)
            logger.warning(f"Rules engine blocked trade before agent: {reasons}")
            return

        user_message = f"""
ATS signal confirmed. Evaluate and execute or reject this trade.

SIGNAL:
- Direction: {signal.direction.upper()}
- Entry price: {entry_price}
- Trigger bar high: {signal.trigger_high}
- Trigger bar low: {signal.trigger_low}
- ATR: {signal.atr}
- Stop price (1.5 × ATR): {stop_price}
- ATS bar time: {signal.ats_bar_time}

SIZING (pre-calculated):
- Contracts: {sizing['contracts']} MES
- Stop distance: {sizing['stop_distance_points']} points ({sizing['stop_distance_ticks']} ticks)
- Dollar risk: ${sizing['actual_dollar_risk']} ({sizing['risk_pct_actual']}% of account)
- 2R target: {targets['target_2r']}

ACCOUNT STATE:
- Balance: ${rules_status['balance']:,.2f}
- Drawdown remaining: ${rules_status['drawdown_remaining']:,.2f}
- Today's profit: ${rules_status['today_profit']:,.2f}
- Cycle profit: ${rules_status['cycle_profit']:,.2f} / ${rules_status['profit_target']:,.0f} target
- Trades today: {rules_status['trades_today']}

RULES: Pre-trade check PASSED. All MFFU rules are clear to trade.

Instrument: {INSTRUMENT}
"""

        logger.info("Calling Claude agent for trade decision...")

        messages = [{"role": "user", "content": user_message}]
        response = self.client.messages.create(
            model      = "claude-haiku-4-5-20251001",
            max_tokens = 1024,
            system     = SYSTEM_PROMPT,
            tools      = self._tools,
            messages   = messages,
        )

        self._handle_response(response, signal, entry_price, sizing, targets)

    # ── Response handler ───────────────────────────────────

    def _handle_response(self, response, signal: PendingSignal,
                          entry_price: float, sizing: dict, targets: dict):
        for block in response.content:
            if block.type == "tool_use":
                tool  = block.name
                inp   = block.input

                if tool == "place_entry_order":
                    logger.info(f"Agent decision: PLACE ORDER | {inp['reasoning']}")
                    self._execute_order(inp, signal, entry_price, sizing, targets)

                elif tool == "reject_trade":
                    logger.info(f"Agent decision: REJECT | {inp['reason']}")

            elif block.type == "text" and block.text:
                logger.info(f"Agent reasoning: {block.text}")

    # ── Order execution ────────────────────────────────────

    def _execute_order(self, order_params: dict, signal: PendingSignal,
                        entry_price: float, sizing: dict, targets: dict):
        action    = order_params["action"]
        contracts = order_params["contracts"]
        stop      = round_to_tick(order_params["stop_price"])

        try:
            result = self.tradovate.place_bracket_order(
                symbol     = INSTRUMENT,
                action     = action,
                qty        = contracts,
                stop_price = stop,
            )
            logger.info(f"Order placed successfully: {result}")

            # Register trade with position monitor
            trade = OpenTrade(
                direction       = signal.direction,
                entry_price     = entry_price,
                stop_price      = stop,
                total_contracts = contracts,
                entry_order_id  = result.get("orderId"),
            )
            self.position_monitor.open_trade(trade)

            # Call trade logging callback if provided
            if self._on_trade_opened:
                self._on_trade_opened(
                    direction=signal.direction,
                    entry_price=entry_price,
                    stop_price=stop,
                    atr=signal.atr,
                    contracts=contracts,
                    target_2r=targets['target_2r'],
                )

        except Exception as e:
            logger.error(f"Order placement failed: {e}", exc_info=True)
