"""Account-aware portfolio-management pass.

One click runs the whole loop: read the Agentic snapshot, pull Bright Data
context for held and candidate names, rank them, hand the ranking to the active
portfolio agent, attempt the plan, and log the run for offline replay.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from ai_trader.agent_runtime import PortfolioAgentRegistry
from ai_trader.config import Settings
from ai_trader.engine import (
    AnalysisRequest,
    BrightDataClient,
    CodexLLM,
    DEFAULT_UNIVERSE,
    SourceWeights,
)
from ai_trader.portfolio import PortfolioPolicy
from ai_trader.replay import ReplayStore
from ai_trader.robinhood import RobinhoodTrader
from ai_trader.utils import money_string


@dataclass(frozen=True)
class ManagePortfolioRequest:
    pool_size: int
    weights: SourceWeights
    use_active_agent: bool = True


class PortfolioManagementEngine:
    def __init__(self, settings: Settings, *, trader: RobinhoodTrader | None = None) -> None:
        self.settings = settings
        self.trader = trader or RobinhoodTrader(settings)
        self.registry = PortfolioAgentRegistry(settings.portfolio_agent_dir)
        self.replay = ReplayStore(settings.replay_log_dir)
        self.bright_data = BrightDataClient(settings)
        self.llm = CodexLLM(settings)

    @property
    def policy(self) -> PortfolioPolicy:
        return PortfolioPolicy(
            cash_reserve_usd=self.settings.portfolio_cash_reserve_usd,
            max_positions=self.settings.portfolio_max_positions,
            max_position_pct=self.settings.portfolio_max_position_pct,
            min_trade_usd=self.settings.portfolio_min_trade_usd,
        )

    def manage_portfolio(self, request_model: ManagePortfolioRequest) -> dict[str, Any]:
        policy = self.policy
        candidates = DEFAULT_UNIVERSE[: request_model.pool_size]

        # 1. Account snapshot.
        snapshot = self.trader.fetch_portfolio_snapshot(candidates)
        held = _held_symbols(snapshot)

        # 2. Context for held plus candidate names.
        universe = held + [symbol for symbol in candidates if symbol not in held]
        context = self.bright_data.collect(universe, max_symbols=max(len(universe), 1))

        # 3. Rank the opportunity set.
        analysis_budget = _investable_budget(snapshot, policy, self.settings)
        analysis = self.llm.recommend(
            AnalysisRequest(
                budget=analysis_budget,
                pool_size=len(universe),
                weights=request_model.weights,
            ),
            context,
        )
        ranking = _clean_ranking(analysis.get("pool"), universe)

        # 4. Plan through the active (or built-in) portfolio agent.
        if request_model.use_active_agent:
            planner, agent_info = self.registry.load_active_planner()
        else:
            from ai_trader.portfolio import BUILTIN_VERSION_ID, plan_portfolio

            planner, agent_info = plan_portfolio, {"version_id": BUILTIN_VERSION_ID, "source": "builtin_forced"}

        try:
            plan = planner(snapshot, ranking, policy)
        except Exception as exc:
            plan = {
                "decision": "no_trade",
                "summary": "The active portfolio agent raised an error, so no orders were generated.",
                "actions": [],
                "ordered_actions": [],
                "warnings": [f"Portfolio agent error: {exc}"],
                "policy": policy.to_dict(),
            }
            agent_info = {**agent_info, "error": str(exc)}

        # 5. Review and attempt the plan.
        execution = self.trader.execute_management_plan(plan)

        result: dict[str, Any] = {
            "account_snapshot": snapshot,
            "ranking": ranking,
            "management_plan": plan,
            "execution": execution,
            "policy": policy.to_dict(),
            "meta": {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "use_active_agent": request_model.use_active_agent,
                "active_agent": agent_info,
                "bright_data_mode": context.get("mode", "bright_data"),
                "candidate_pool": universe,
                "analysis_budget": money_string(analysis_budget),
            },
        }
        result["reasoning_trace"] = self._reasoning_trace(snapshot, ranking, plan, execution, agent_info, context)

        # 6. Log the run for offline replay and SIA scoring.
        try:
            result["run_id"] = self.replay.log_management_run(
                {
                    "account_snapshot": snapshot,
                    "ranking": ranking,
                    "management_plan": plan,
                    "execution": execution,
                    "policy": policy.to_dict(),
                    "agent": agent_info,
                }
            )
        except OSError as exc:
            result["meta"]["replay_error"] = str(exc)

        return result

    def _reasoning_trace(
        self,
        snapshot: dict[str, Any],
        ranking: list[dict[str, Any]],
        plan: dict[str, Any],
        execution: dict[str, Any],
        agent_info: dict[str, Any],
        context: dict[str, Any],
    ) -> list[dict[str, str]]:
        portfolio = snapshot.get("portfolio") or {}
        positions = snapshot.get("positions") or []
        top = ", ".join(str(item.get("symbol")) for item in ranking[:3]) or "none"
        ordered = plan.get("ordered_actions") or []
        return [
            {
                "stage": "snapshot",
                "title": "Account snapshot",
                "detail": (
                    f"{len(positions)} position(s), total value "
                    f"${portfolio.get('total_value', 0)}, cash ${portfolio.get('cash_available', 0)}."
                ),
            },
            {
                "stage": "context",
                "title": "Market context",
                "detail": f"Bright Data mode {context.get('mode', 'bright_data')} across {len(context.get('symbols', []) or [])} symbol(s).",
            },
            {
                "stage": "ranking",
                "title": "Opportunity ranking",
                "detail": f"{len(ranking)} ranked candidate(s). Leaders: {top}.",
            },
            {
                "stage": "planning",
                "title": "Portfolio agent",
                "detail": (
                    f"Agent {agent_info.get('version_id')} ({agent_info.get('source')}) decided "
                    f"{plan.get('decision', 'no_trade')}: {plan.get('summary', '--')}"
                ),
            },
            {
                "stage": "execution",
                "title": "Execution",
                "detail": f"{len(ordered)} ordered action(s), execution status {execution.get('status', 'unknown')}.",
            },
        ]


def _held_symbols(snapshot: dict[str, Any]) -> list[str]:
    symbols: list[str] = []
    for position in snapshot.get("positions") or []:
        if not isinstance(position, dict):
            continue
        symbol = str(position.get("symbol") or "").strip().upper()
        if symbol and symbol not in symbols:
            symbols.append(symbol)
    return symbols


def _investable_budget(snapshot: dict[str, Any], policy: PortfolioPolicy, settings: Settings) -> Decimal:
    portfolio = snapshot.get("portfolio") or {}
    try:
        total = Decimal(str(portfolio.get("total_value") or portfolio.get("equity") or 0))
    except Exception:
        total = Decimal("0")
    investable = total - policy.cash_reserve_usd
    if investable <= 0:
        return settings.default_budget_usd
    return investable


def _clean_ranking(pool: Any, universe: list[str]) -> list[dict[str, Any]]:
    """Keep the ranking to real symbols with usable scores."""
    ranking: list[dict[str, Any]] = []
    seen: set[str] = set()
    if isinstance(pool, list):
        for item in pool:
            if not isinstance(item, dict):
                continue
            symbol = str(item.get("symbol") or "").strip().upper()
            if not symbol or symbol in seen:
                continue
            try:
                score = float(item.get("score", 0) or 0)
            except (TypeError, ValueError):
                score = 0.0
            seen.add(symbol)
            ranking.append(
                {
                    "symbol": symbol,
                    "score": round(score, 4),
                    "reason": str(item.get("reason") or "").strip() or "No rationale supplied.",
                }
            )

    if not ranking:
        # Even-weight the candidate set rather than returning nothing to plan against.
        step = 0.6 / max(len(universe), 1)
        ranking = [
            {
                "symbol": symbol,
                "score": round(0.7 - index * step, 4),
                "reason": "Neutral fallback ranking: the ranking model returned no usable pool.",
            }
            for index, symbol in enumerate(universe)
        ]

    ranking.sort(key=lambda item: item["score"], reverse=True)
    return ranking
