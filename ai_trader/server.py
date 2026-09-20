from __future__ import annotations

from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ai_trader.config import Settings
from ai_trader.engine import AnalysisRequest, DEFAULT_UNIVERSE, RecommendationEngine, SourceWeights
from ai_trader.portfolio_engine import ManagePortfolioRequest, PortfolioManagementEngine
from ai_trader.robinhood import RobinhoodTrader
from ai_trader.sia import SIAService

STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(title="YOLO STREET", version="0.2.0")
settings = Settings.load()
engine = RecommendationEngine(settings)
trader = RobinhoodTrader(settings)
portfolio_engine = PortfolioManagementEngine(settings, trader=trader)
sia_service = SIAService(settings)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


class WeightsPayload(BaseModel):
    reddit: float = Field(default=0.35, ge=0, le=1)
    x: float = Field(default=0.25, ge=0, le=1)
    realtime: float = Field(default=0.40, ge=0, le=1)


class AnalyzePayload(BaseModel):
    budget: str = "5"
    pool_size: int = Field(default=5, ge=1, le=10)
    weights: WeightsPayload = Field(default_factory=WeightsPayload)


class TradePayload(BaseModel):
    recommendation: dict[str, Any]
    execute: bool = False
    confirm_phrase: str = ""


class ManagePortfolioPayload(BaseModel):
    pool_size: int = Field(default=6, ge=1, le=10)
    weights: WeightsPayload = Field(default_factory=WeightsPayload)
    use_active_agent: bool = True


class ActivateAgentPayload(BaseModel):
    version_id: str


class SiaRunPayload(BaseModel):
    max_generations: int = Field(default=1, ge=1, le=20)
    build_replay_first: bool = True


@app.get("/")
def landing() -> FileResponse:
    """Marketing landing page. The operating dashboard lives at /app."""
    return FileResponse(STATIC_DIR / "landing.html")


@app.get("/app")
def dashboard() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
def health() -> dict[str, Any]:
    return trader.doctor()


@app.get("/api/config")
def config() -> dict[str, Any]:
    return {
        "default_budget_usd": str(settings.default_budget_usd),
        "max_budget_usd": str(settings.max_budget_usd),
        "bright_data_configured": bool(settings.bright_data_api_key),
        "bright_data_endpoint": settings.bright_data_endpoint,
        "portfolio_policy": {
            "cash_reserve_usd": str(settings.portfolio_cash_reserve_usd),
            "max_positions": settings.portfolio_max_positions,
            "max_position_pct": str(settings.portfolio_max_position_pct),
            "min_trade_usd": str(settings.portfolio_min_trade_usd),
        },
        "portfolio_candidate_pool_size": settings.portfolio_candidate_pool_size,
        "active_portfolio_agent": portfolio_engine.registry.list_agents()["current"],
        "sia": sia_service.status(),
    }


@app.post("/api/analyze")
def analyze(payload: AnalyzePayload) -> dict[str, Any]:
    try:
        budget = Decimal(payload.budget)
    except InvalidOperation as exc:
        raise HTTPException(status_code=400, detail="budget must be a decimal value") from exc
    if budget <= 0 or budget > settings.max_budget_usd:
        raise HTTPException(status_code=400, detail=f"budget must be > 0 and <= {settings.max_budget_usd}")

    request_model = AnalysisRequest(
        budget=budget,
        pool_size=payload.pool_size,
        weights=SourceWeights(
            reddit=payload.weights.reddit,
            x=payload.weights.x,
            realtime=payload.weights.realtime,
        ),
    )
    return engine.analyze(request_model)


@app.post("/api/trade")
def trade(payload: TradePayload) -> dict[str, Any]:
    return engine.trade(
        payload.recommendation,
        execute=payload.execute,
        confirm_phrase=payload.confirm_phrase,
    )


@app.post("/api/manage-portfolio")
def manage_portfolio(payload: ManagePortfolioPayload) -> dict[str, Any]:
    request_model = ManagePortfolioRequest(
        pool_size=payload.pool_size,
        weights=SourceWeights(
            reddit=payload.weights.reddit,
            x=payload.weights.x,
            realtime=payload.weights.realtime,
        ),
        use_active_agent=payload.use_active_agent,
    )
    return portfolio_engine.manage_portfolio(request_model)


@app.get("/api/portfolio-snapshot")
def portfolio_snapshot() -> dict[str, Any]:
    universe = DEFAULT_UNIVERSE[: settings.portfolio_candidate_pool_size]
    snapshot = trader.fetch_portfolio_snapshot(universe)
    return {
        "account_snapshot": snapshot,
        "meta": {
            "active_agent": portfolio_engine.registry.list_agents()["current"],
            "available_agent_setting": True,
            "policy": {
                "cash_reserve_usd": str(settings.portfolio_cash_reserve_usd),
                "max_positions": settings.portfolio_max_positions,
                "max_position_pct": str(settings.portfolio_max_position_pct),
                "min_trade_usd": str(settings.portfolio_min_trade_usd),
            },
        },
    }


@app.get("/api/portfolio-agents")
def portfolio_agents() -> dict[str, Any]:
    return portfolio_engine.registry.list_agents()


@app.post("/api/portfolio-agents/activate")
def activate_portfolio_agent(payload: ActivateAgentPayload) -> dict[str, Any]:
    try:
        return portfolio_engine.registry.activate(payload.version_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/portfolio-agents/rollback")
def rollback_portfolio_agent() -> dict[str, Any]:
    try:
        return portfolio_engine.registry.rollback()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/sia/status")
def sia_status() -> dict[str, Any]:
    return sia_service.status()


@app.post("/api/sia/replay-build")
def sia_replay_build() -> dict[str, Any]:
    result = sia_service.build_replay_dataset()
    return {
        **result,
        "reasoning_trace": sia_service.reasoning_trace("build", result),
    }


@app.post("/api/sia/run")
def sia_run(payload: SiaRunPayload) -> dict[str, Any]:
    try:
        result = sia_service.run(
            max_generations=payload.max_generations,
            build_replay_first=payload.build_replay_first,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {
        **result,
        "reasoning_trace": sia_service.reasoning_trace("run", result),
    }
