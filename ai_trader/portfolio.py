"""Built-in buy / hold / trim / exit planner.

This is the default portfolio manager. SIA-generated managers are drop-in
replacements: they only have to expose ``plan_portfolio(snapshot, ranking, policy)``
and return the same dict shape.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from ai_trader.utils import money_string

BUILTIN_VERSION_ID = "builtin-default-v1"


@dataclass(frozen=True)
class PortfolioPolicy:
    cash_reserve_usd: Decimal = Decimal("5")
    max_positions: int = 5
    max_position_pct: Decimal = Decimal("0.45")
    min_trade_usd: Decimal = Decimal("5")

    def to_dict(self) -> dict[str, Any]:
        return {
            "cash_reserve_usd": str(self.cash_reserve_usd),
            "max_positions": self.max_positions,
            "max_position_pct": str(self.max_position_pct),
            "min_trade_usd": str(self.min_trade_usd),
        }


def to_decimal(value: Any, default: str = "0") -> Decimal:
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal(default)


def plan_portfolio(
    snapshot: dict[str, Any],
    ranking: list[dict[str, Any]],
    policy: PortfolioPolicy,
) -> dict[str, Any]:
    """Turn an account snapshot plus a ranked opportunity set into one plan."""
    portfolio = snapshot.get("portfolio") or {}
    positions = [p for p in (snapshot.get("positions") or []) if isinstance(p, dict)]

    held: dict[str, Decimal] = {}
    for position in positions:
        symbol = str(position.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        held[symbol] = held.get(symbol, Decimal("0")) + to_decimal(
            position.get("market_value", position.get("current_value"))
        )

    total_value = to_decimal(portfolio.get("total_value", portfolio.get("equity")))
    cash_available = to_decimal(portfolio.get("cash_available", portfolio.get("buying_power")))
    if total_value <= 0:
        total_value = sum(held.values(), Decimal("0")) + cash_available

    warnings: list[str] = []
    investable = total_value - policy.cash_reserve_usd
    if investable <= 0:
        return {
            "decision": "no_trade",
            "summary": (
                f"Total account value ${money_string(total_value)} is at or below the "
                f"${money_string(policy.cash_reserve_usd)} cash reserve, so no capital is deployable."
            ),
            "actions": [
                _action("hold", symbol, current, current, Decimal("0"), "Held below the cash-reserve floor.")
                for symbol, current in sorted(held.items())
            ],
            "ordered_actions": [],
            "warnings": ["Investable capital is zero after the cash reserve."],
            "policy": policy.to_dict(),
        }

    targets = _target_allocations(ranking, held, investable, policy)
    if not targets and not held:
        return {
            "decision": "no_trade",
            "summary": "No ranked candidates and no existing positions, so there is nothing to manage.",
            "actions": [],
            "ordered_actions": [],
            "warnings": ["Ranking returned no usable candidates."],
            "policy": policy.to_dict(),
        }

    reasons = {
        str(item.get("symbol") or "").upper(): str(item.get("reason") or "").strip()
        for item in ranking
        if isinstance(item, dict)
    }

    actions: list[dict[str, Any]] = []
    for symbol in sorted(set(held) | set(targets)):
        current = held.get(symbol, Decimal("0"))
        target = targets.get(symbol, Decimal("0"))
        delta = target - current
        size = abs(delta)

        if target <= 0 and current > 0:
            actions.append(
                _action(
                    "exit",
                    symbol,
                    current,
                    Decimal("0"),
                    current,
                    reasons.get(symbol) or "Dropped out of the ranked target set.",
                )
            )
            continue

        if size < policy.min_trade_usd:
            note = (
                f"Within ${money_string(policy.min_trade_usd)} of target; "
                "rebalancing would cost more than it corrects."
            )
            actions.append(_action("hold", symbol, current, target, Decimal("0"), reasons.get(symbol) or note))
            continue

        if delta > 0:
            actions.append(
                _action("buy", symbol, current, target, size, reasons.get(symbol) or "Ranked above the current weight.")
            )
        else:
            actions.append(
                _action("trim", symbol, current, target, size, reasons.get(symbol) or "Position is above its target weight.")
            )

    # Sells free up cash, so they must be attempted before buys.
    sells = [a for a in actions if a["action"] in {"exit", "trim"}]
    buys = [a for a in actions if a["action"] == "buy"]

    spendable = cash_available - policy.cash_reserve_usd + sum(
        to_decimal(a["order_dollar"]) for a in sells
    )
    approved_buys: list[dict[str, Any]] = []
    for buy in sorted(buys, key=lambda a: to_decimal(a["order_dollar"]), reverse=True):
        order = to_decimal(buy["order_dollar"])
        if order <= spendable:
            approved_buys.append(buy)
            spendable -= order
            continue
        if spendable >= policy.min_trade_usd:
            buy["order_dollar"] = float(spendable)
            buy["target_dollar"] = float(to_decimal(buy["current_dollar"]) + spendable)
            buy["reason"] = f"{buy['reason']} Sized down to the available cash after the reserve."
            approved_buys.append(buy)
            spendable = Decimal("0")
            continue
        buy["action"] = "hold"
        buy["order_dollar"] = 0.0
        buy["target_dollar"] = buy["current_dollar"]
        buy["reason"] = f"{buy['reason']} Skipped: not enough cash above the reserve."
        warnings.append(f"{buy['symbol']} buy skipped for insufficient cash above the reserve.")

    ordered_actions = sells + approved_buys
    actions = [a for a in actions if a["action"] != "buy"] + approved_buys
    actions.sort(key=lambda a: (a["action"] == "hold", a["symbol"]))

    if len(targets) > policy.max_positions:
        warnings.append(f"Target set truncated to {policy.max_positions} positions.")

    decision = "manage" if ordered_actions else "no_trade"
    summary = (
        f"{len(ordered_actions)} order(s) across {len(targets)} target position(s) "
        f"against ${money_string(investable)} investable of ${money_string(total_value)} total."
        if ordered_actions
        else "Every position is already within tolerance of its target weight."
    )
    return {
        "decision": decision,
        "summary": summary,
        "actions": actions,
        "ordered_actions": ordered_actions,
        "warnings": warnings,
        "policy": policy.to_dict(),
        "targets": {symbol: float(value) for symbol, value in sorted(targets.items())},
    }


def _target_allocations(
    ranking: list[dict[str, Any]],
    held: dict[str, Decimal],
    investable: Decimal,
    policy: PortfolioPolicy,
) -> dict[str, Decimal]:
    scored: list[tuple[str, Decimal]] = []
    seen: set[str] = set()
    for item in ranking:
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("symbol") or "").strip().upper()
        if not symbol or symbol in seen:
            continue
        score = to_decimal(item.get("score"))
        if score <= 0:
            continue
        seen.add(symbol)
        scored.append((symbol, score))

    scored.sort(key=lambda pair: pair[1], reverse=True)
    scored = scored[: policy.max_positions]
    if not scored:
        return {}

    total_score = sum(score for _, score in scored)
    cap = investable * policy.max_position_pct
    targets: dict[str, Decimal] = {}
    for symbol, score in scored:
        raw = investable * score / total_score
        targets[symbol] = min(raw, cap).quantize(Decimal("0.01"))
    return targets


def _action(
    action: str,
    symbol: str,
    current: Decimal,
    target: Decimal,
    order: Decimal,
    reason: str,
) -> dict[str, Any]:
    return {
        "action": action,
        "symbol": symbol,
        "current_dollar": float(current),
        "target_dollar": float(target),
        "order_dollar": float(order),
        "side": "buy" if action == "buy" else ("sell" if action in {"trim", "exit"} else "none"),
        "reason": reason or "No rationale supplied.",
    }
