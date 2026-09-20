"""ASGI entrypoint for hosts that autodetect a FastAPI app at the repo root.

Serverless filesystems are read-only apart from /tmp, so every writable store is
redirected there. /tmp does not survive between cold starts, so accounts and
decisions created on a hosted instance are demo-lifetime only. A durable
deployment needs a real database; see docs/SYSTEM-DESIGN.md.
"""

from __future__ import annotations

import os

os.environ.setdefault("REPLAY_LOG_DIR", "/tmp/ai_trader/replay")
os.environ.setdefault("PORTFOLIO_AGENT_DIR", "/tmp/ai_trader/portfolio_agents")
os.environ.setdefault("ACCOUNT_DIR", "/tmp/ai_trader/accounts")
os.environ.setdefault("DECISION_DIR", "/tmp/ai_trader/decisions")
os.environ.setdefault("SESSION_SECURE_COOKIE", "true")
os.environ.setdefault("MAX_BUDGET_USD", "5")
os.environ.setdefault("DEFAULT_BUDGET_USD", "5")

from ai_trader.server import app  # noqa: E402

__all__ = ["app"]
