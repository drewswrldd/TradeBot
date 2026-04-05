# ATS Trading Agent

Autonomous futures trading agent for MES (Micro E-mini S&P 500).
Executes the AutoTrend System (ATS) Strategy 1 on a MyFundedFutures 50K evaluation account.

## Architecture

TradingView ATS webhook → Flask receiver → Bar confirmation monitor → MFFU Rules Engine → Claude AI Agent → Tradovate API

## Key Rules (NEVER violate)

- Max trailing drawdown: $2,000 (EOD, from high-water mark)
- Profit target: $3,000
- Max contracts: 3 MES (configured in .env)
- Consistency: no single day > 50% of profit target ($1,500)
- Tier 1 news blackout: flat 2 min before/after event
- Risk per trade: ≤ 2% of account balance
- Max 200 trades/day

## ATS Strategy 1 Logic

**Long entry:**
1. ATS turns blue (webhook received)
2. Wait for next bar to trade ABOVE trigger bar's high
3. Enter market long, stop at last swing low
4. Exit 50% at 2R profit
5. Exit remaining 50% when ATS turns red (bar close)

**Short entry:** Mirror of above (red → below trigger low → stop at swing high)

## Environment

- Python 3.11+
- Flask 3.1 on port 5001
- Tradovate demo env for testing, live for funded account
- PostgreSQL for trade logging (optional, not wired in v1)
- Claude claude-sonnet-4-20250514 for agent decisions

## File Structure

```
app.py                  # Flask entry point + webhook handler
config.py               # All env vars and constants
tradovate/
  client.py             # REST API wrapper (auth, orders, positions)
  websocket.py          # Live price + bar data WebSocket
rules/
  mffu_rules.py         # MFFU rules engine (hard gate)
  risk.py               # Position sizing (2% rule)
  news_calendar.py      # Tier 1 event calendar + blackout
monitor/
  bar_monitor.py        # Next-bar entry confirmation
  position_monitor.py   # 2R exit + ATS reversal exit
agent/
  agent.py              # Claude agent with place/reject tools
```

## Running

```bash
cp .env.example .env
# fill in credentials
pip install -r requirements.txt
python app.py
```

## TradingView Webhook Payload

```json
{
  "secret":        "{{WEBHOOK_SECRET}}",
  "direction":     "long",
  "color":         "blue",
  "trigger_high":  5284.50,
  "trigger_low":   5271.25,
  "swing_extreme": 5265.00,
  "bar_time":      "{{time}}",
  "close_price":   5278.75
}
```

## CRITICAL

- Always run on TRADOVATE_ENV=demo until fully tested
- Never remove the MFFU rules engine pre-trade check
- The /flatten endpoint is the emergency kill switch
- Monitor logs/agent.log during all live sessions
