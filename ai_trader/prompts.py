from __future__ import annotations

import json
from decimal import Decimal

from ai_trader.utils import money_string


def recommendation_prompt(objective: str, budget: Decimal) -> str:
    return f"""
Use the `robinhood-trading` MCP inside Codex.

Objective: {objective}
Budget cap: ${money_string(budget)}

Rules:
- Recommend exactly one U.S. long equity idea or return "no_trade".
- Stay at or below the budget cap.
- Prefer liquid, well-known, non-leveraged names.
- Do not use OTC, penny stocks, options, crypto, margin, leverage, or short exposure.
- Use current market context and recent news if web search is available.
- Verify tradability and quotes with Robinhood tools before deciding.
- If the symbol is not fractionally tradable and a whole share would exceed the budget, return "no_trade".
- Prefer no trade over a weak trade.

Return valid JSON only:
{{
  "decision": "buy" | "no_trade",
  "symbol": "string or null",
  "dollar_amount": number,
  "thesis": ["short bullet", "short bullet", "short bullet"],
  "risks": ["short bullet", "short bullet"],
  "why_now": "one short paragraph",
  "order_notes": "one short paragraph"
}}
""".strip()


def preview_prompt(objective: str, budget: Decimal) -> str:
    return f"""
Use the `robinhood-trading` MCP inside Codex to:

1. Recommend exactly one long U.S. equity idea for this objective: {objective}
2. Keep the order at or below ${money_string(budget)}
3. Check tradability and quote context
4. Call `review_equity_order`

Do not place an order.
Return valid JSON only:
{{
  "decision": "previewed" | "no_trade" | "blocked",
  "symbol": "string or null",
  "dollar_amount": number,
  "warnings": ["warning or empty if none"],
  "review_summary": "short paragraph",
  "thesis": ["short bullet", "short bullet", "short bullet"],
  "next_step": "short sentence"
}}
""".strip()


def live_prompt(objective: str, budget: Decimal) -> str:
    return f"""
Use the `robinhood-trading` MCP inside Codex to execute a cautious live workflow:

1. Recommend exactly one long U.S. equity idea for this objective: {objective}
2. Keep the order at or below ${money_string(budget)}
3. Verify tradability and quotes
4. Review the order with `review_equity_order`
5. Stop and show me the planned order
6. Wait for my explicit `CONFIRM` reply in the Codex session before placing the order
7. Only after I confirm, place one long-only equity order with `place_equity_order`

If there is no good trade, return `no_trade` and stop.
After the workflow completes, return valid JSON only:
{{
  "decision": "submitted" | "no_trade" | "blocked",
  "symbol": "string or null",
  "dollar_amount": number,
  "summary": "short paragraph",
  "warnings": ["warning or empty if none"]
}}
""".strip()


def launcher_note() -> str:
    return """
This project launches an interactive Codex session because Robinhood MCP tool calls are not completing reliably in non-interactive `codex exec` mode.
Use the opened Codex session to approve account or tool actions and, for live mode, type `CONFIRM` only if you want the order placed.
""".strip()


def direct_search_prompt(query: str) -> str:
    return f"""
Use the `robinhood-trading` MCP only.

Call `search` for {query} and return only JSON:
{{
  "query": "{query}",
  "results": []
}}
""".strip()


def portfolio_snapshot_prompt(symbols: list[str]) -> str:
    joined = ", ".join(symbols) if symbols else "AAPL, MSFT, NVDA"
    return f"""
Use the `robinhood-trading` MCP only.

Collect one account snapshot for autonomous portfolio management:
1. Call `get_accounts`
2. Identify the Robinhood Agentic account that can place trades
3. Call `get_portfolio`
4. Call `get_equity_positions`
5. Call `get_equity_orders`
6. Call `get_equity_quotes` for these symbols: {joined}
7. Call `get_equity_tradability` for these symbols: {joined}

Return valid JSON only:
{{
  "agentic_account": {{
    "account_id": "string or null",
    "account_number_masked": "string or null"
  }},
  "portfolio": {{
    "total_value": number,
    "buying_power": number,
    "cash_available": number
  }},
  "positions": [
    {{
      "symbol": "string",
      "quantity": number,
      "market_value": number,
      "cost_basis": number,
      "current_price": number
    }}
  ],
  "quotes": {{
    "AAPL": {{
      "last_trade_price": number,
      "previous_close": number
    }}
  }},
  "tradability": {{
    "AAPL": {{
      "tradeable": true,
      "fractional_tradability": true
    }}
  }},
  "recent_orders": [
    {{
      "symbol": "string",
      "side": "buy" | "sell",
      "status": "string",
      "submitted_at": "ISO timestamp or null",
      "filled_at": "ISO timestamp or null"
    }}
  ],
  "warnings": ["warning or empty if none"]
}}
""".strip()


def portfolio_execution_prompt(plan: dict[str, object]) -> str:
    serialized = json.dumps(plan, indent=2)[:16000]
    return f"""
Use the `robinhood-trading` MCP only.

Execute exactly one autonomous portfolio-management pass for this reviewed plan:
{serialized}

Rules:
- Long U.S. equities only.
- Process sells before buys.
- For `exit`, sell the full position quantity if needed.
- For `trim`, reduce the position by approximately the planned dollar amount using a supported review/place flow.
- For `buy`, use the planned dollar amount.
- Review every order first.
- If Robinhood asks for explicit confirmation to place a reviewed order, reply with `CONFIRM` and continue in the same run.
- Skip any unsupported or unsafe order instead of improvising.
- Stop after the full plan has been attempted once.

Return valid JSON only:
{{
  "status": "submitted" | "blocked" | "no_op" | "failed",
  "reviewed_orders": [
    {{
      "symbol": "string",
      "side": "buy" | "sell",
      "requested_dollar_amount": number,
      "summary": "short paragraph"
    }}
  ],
  "placed_orders": [
    {{
      "symbol": "string",
      "side": "buy" | "sell",
      "dollar_amount": number,
      "order_id": "string or null"
    }}
  ],
  "skipped_orders": [
    {{
      "symbol": "string",
      "side": "buy" | "sell",
      "reason": "short reason"
    }}
  ],
  "warnings": ["warning or empty if none"],
  "summary": "short paragraph"
}}
""".strip()
