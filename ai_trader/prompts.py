from __future__ import annotations

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
