"""Baseline target agent for the portfolio-management replay task.

SIA mutates this file. It must keep the same entrypoint signature.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ai_trader.portfolio import PortfolioPolicy, plan_portfolio


def solve(case: dict) -> dict:
    """Return the management plan for one replay case."""
    payload = case.get("input", {})
    return plan_portfolio(
        payload.get("account_snapshot", {}),
        payload.get("ranking", []),
        PortfolioPolicy(),
    )
