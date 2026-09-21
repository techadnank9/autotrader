"""Today's picks: one shared research run, cached, served to every user.

Research covers the same stocks for everyone and takes 30-90 seconds, so it runs
once and is cached rather than once per page view. A scheduled run each weekday
morning keeps it warm; a stale cache refreshes itself on the next visit.
"""

from __future__ import annotations

import json
import secrets
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

from ai_trader.engine import DEFAULT_UNIVERSE, AnalysisRequest, SourceWeights

REFRESH_COOLDOWN_SECONDS = 600


class FilePicksStore:
    def __init__(self, root: str | Path) -> None:
        self.path = Path(root) / "picks_latest.json"

    def latest(self) -> dict[str, Any] | None:
        if not self.path.is_file():
            return None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        return data if isinstance(data, dict) else None

    def save(self, run: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(run), encoding="utf-8")


def _sources(claim_ids: list[str], claims_by_id: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for cid in claim_ids:
        c = claims_by_id.get(cid)
        if not c or not c.get("urls"):
            continue
        out.append({"title": c.get("headline"), "url": c["urls"][0],
                    "outlet": (c.get("domains") or [""])[0], "date": c.get("latest")})
    return out[:4]


class PicksService:
    def __init__(self, engine: Any, store: Any, *, ttl_hours: float, budget: Decimal) -> None:
        self.engine = engine
        self.store = store
        self.ttl_seconds = ttl_hours * 3600
        self.budget = budget

    def is_fresh(self, run: dict[str, Any] | None) -> bool:
        return bool(run) and (time.time() - float(run.get("created_at", 0))) < self.ttl_seconds

    def run_now(self) -> dict[str, Any]:
        started = time.time()
        analysis = self.engine.analyze(AnalysisRequest(
            budget=self.budget, pool_size=len(DEFAULT_UNIVERSE),
            weights=SourceWeights(0.35, 0.25, 0.40),
        ))
        research = analysis.get("research") or {}
        claims_by_id = {c["claim_id"]: c for cs in (research.get("claims") or {}).values() for c in cs}
        live = analysis.get("meta", {}).get("research_mode") == "live"
        ranked = analysis.get("meta", {}).get("ranking_mode") == "model"

        picks = [{
            "symbol": p["symbol"],
            "verdict": p.get("verdict", "watch"),
            "score": p.get("score", 0.0),
            "summary": p.get("summary") or p.get("reason") or "",
            "risks": p.get("risks") or [],
            "sources": _sources(p.get("claim_ids") or [], claims_by_id),
        } for p in (analysis.get("pool") or [])]

        run = {
            "run_id": f"run_{time.strftime('%Y%m%d%H%M%S', time.gmtime())}_{secrets.token_hex(3)}",
            "created_at": time.time(),
            "took_seconds": round(time.time() - started, 1),
            "status": "ok" if (live and ranked) else "unavailable",
            "message": None if (live and ranked) else "Today's research is not available yet.",
            "picks": picks,
            "articles_read": research.get("raw_hits", 0),
        }
        self.store.save(run)
        return run

    def latest_or_run(self) -> dict[str, Any]:
        run = self.store.latest()
        return run if self.is_fresh(run) else self.run_now()

    def refresh(self) -> dict[str, Any]:
        run = self.store.latest()
        if run and time.time() - float(run.get("created_at", 0)) < REFRESH_COOLDOWN_SECONDS:
            return run  # picks are shared; do not let refreshes hammer research
        return self.run_now()
