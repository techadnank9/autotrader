from __future__ import annotations

from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ai_trader.config import Settings
from ai_trader.engine import AnalysisRequest, RecommendationEngine, SourceWeights
from ai_trader.robinhood import RobinhoodTrader

STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(title="AI Trader", version="0.2.0")
settings = Settings.load()
engine = RecommendationEngine(settings)
trader = RobinhoodTrader(settings)

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


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
def health() -> dict[str, Any]:
    return trader.doctor()


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
