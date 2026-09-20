"""ASGI entrypoint for hosts that autodetect a FastAPI app at the repo root.

Serverless filesystems are read-only apart from /tmp, so the replay store and the
agent registry are redirected there. Both are ephemeral, which is correct for a
demo surface: a hosted instance has no Codex binary and therefore no broker access.
"""

from __future__ import annotations

import os

os.environ.setdefault("REPLAY_LOG_DIR", "/tmp/ai_trader/replay")
os.environ.setdefault("PORTFOLIO_AGENT_DIR", "/tmp/ai_trader/portfolio_agents")
os.environ.setdefault("MAX_BUDGET_USD", "5")
os.environ.setdefault("DEFAULT_BUDGET_USD", "5")

from ai_trader.server import app  # noqa: E402

__all__ = ["app"]
