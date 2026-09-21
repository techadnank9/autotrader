from __future__ import annotations

from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from fastapi import Cookie, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ai_trader.accounts import AccountError, SessionSigner, UserStore, demo_user
from ai_trader.brokers import BrokerError, build_broker
from ai_trader.config import Settings
from ai_trader.decisions import Decision, DecisionClosed, DecisionService, DecisionStore
from ai_trader.engine import AnalysisRequest, DEFAULT_UNIVERSE, RecommendationEngine, SourceWeights
from ai_trader.portfolio_engine import ManagePortfolioRequest, PortfolioManagementEngine
from ai_trader.robinhood import RobinhoodTrader
from ai_trader.sia import SIAService
from ai_trader.telegram import TelegramClient, parse_callback

STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(title="YOLO STREET", version="0.2.0")
settings = Settings.load()
engine = RecommendationEngine(settings)
trader = RobinhoodTrader(settings)
portfolio_engine = PortfolioManagementEngine(settings, trader=trader)
sia_service = SIAService(settings)
telegram = TelegramClient(settings)


broker = build_broker(settings)


def _execute_decision(decision: Decision) -> dict[str, Any]:
    """The only path from an approved decision to a real order.

    The decision id doubles as the broker's client_order_id, so even a duplicated
    call cannot create a second order.
    """
    if broker.configured:
        return broker.place_notional_buy(
            decision.symbol, Decimal(decision.amount_usd), client_order_id=decision.decision_id
        )
    return engine.trade(
        {"symbol": decision.symbol, "dollar_amount": decision.amount_usd},
        execute=True,
        confirm_phrase="CONFIRM",
    )


def _build_stores():
    """Postgres when DATABASE_URL is set, JSON files otherwise."""
    if settings.database_url:
        try:
            from ai_trader.db import Database, PostgresDecisionStore, PostgresUserStore

            database = Database(settings.database_url)
            return PostgresUserStore(database), PostgresDecisionStore(database), "postgres"
        except Exception as exc:  # a broken DSN must not take the whole app down
            print(f"Postgres unavailable, falling back to file stores: {exc}")
    return UserStore(settings.account_dir), DecisionStore(settings.decision_dir), "files"


users, decision_store, STORAGE_BACKEND = _build_stores()
decisions = DecisionService(decision_store, executor=_execute_decision)
sessions = SessionSigner(settings.session_secret)
SESSION_COOKIE = "aitrader_session"


def _current_user(token: str | None):
    user_id = sessions.verify(token)
    if not user_id:
        return None
    return users.get(user_id)


def _set_session(response: Response, user_id: str) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        sessions.issue(user_id),
        max_age=60 * 60 * 24 * 14,
        httponly=True,
        samesite="lax",
        secure=settings.session_secure_cookie,
        path="/",
    )

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


class AnswerPayload(BaseModel):
    decision_id: str
    approved: bool


class ProposePayload(BaseModel):
    pool_size: int = Field(default=5, ge=1, le=10)
    budget: str | None = None
    notify: bool = True


class SiaRunPayload(BaseModel):
    max_generations: int = Field(default=1, ge=1, le=20)
    build_replay_first: bool = True


@app.get("/")
def landing() -> FileResponse:
    """Marketing landing page. The operating dashboard lives at /app."""
    return FileResponse(STATIC_DIR / "landing.html")


@app.get("/login")
def login_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "auth.html")


@app.get("/app")
def dashboard(aitrader_session: str | None = Cookie(default=None)):
    """The dashboard needs a session. Signing in is one click via Skip."""
    if _current_user(aitrader_session) is None:
        return RedirectResponse("/login", status_code=303)
    return FileResponse(STATIC_DIR / "dashboard.html")


@app.get("/profile")
def profile_page(aitrader_session: str | None = Cookie(default=None)):
    if _current_user(aitrader_session) is None:
        return RedirectResponse("/login", status_code=303)
    return FileResponse(STATIC_DIR / "profile.html")


@app.get("/legacy")
def legacy_dashboard() -> FileResponse:
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
        "storage_backend": STORAGE_BACKEND,
        "capabilities": {
            "research_providers": engine.research.providers,
            "ranking_model": "claude-opus-5" if engine.ranker.enabled else None,
            "broker": broker.name,
            "broker_mode": ("live" if settings.alpaca_live else "paper") if broker.configured else None,
            "telegram": telegram.configured,
        },
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
    if broker.configured:
        try:
            snapshot = {
                "agentic_account": {"account_number_masked": None},
                "portfolio": broker.account(),
                "positions": broker.positions(),
                "warnings": [],
                "source": broker.name,
            }
            snapshot["agentic_account"]["account_number_masked"] = snapshot["portfolio"].get("account_number_masked")
        except BrokerError as exc:
            snapshot = {"portfolio": {}, "positions": [], "warnings": [str(exc)], "source": broker.name}
        return {"account_snapshot": snapshot, "meta": {"active_agent": portfolio_engine.registry.list_agents()["current"]}}
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


def _require_user(token: str | None):
    user = _current_user(token)
    if user is None:
        raise HTTPException(status_code=401, detail="Sign in to use this.")
    return user


@app.post("/api/decisions/answer")
def answer_decision(
    payload: AnswerPayload,
    aitrader_session: str | None = Cookie(default=None),
) -> dict[str, Any]:
    """Approve or skip from the dashboard, through the same gate Telegram uses."""
    user = _require_user(aitrader_session)
    try:
        decision = decisions.answer(
            payload.decision_id, approved=payload.approved, responder=f"dashboard:{user.user_id}"
        )
    except DecisionClosed as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"decision": decision.to_dict()}


@app.get("/api/telegram/status")
def telegram_status() -> dict[str, Any]:
    return telegram.status()


@app.get("/api/decisions")
def list_decisions(aitrader_session: str | None = Cookie(default=None)) -> dict[str, Any]:
    _require_user(aitrader_session)
    return {
        "open": [d.to_dict() for d in decision_store.open_decisions()],
        "recent": [d.to_dict() for d in decision_store.list(limit=20)],
    }


@app.post("/api/decisions/propose")
def propose_decision(
    payload: ProposePayload,
    aitrader_session: str | None = Cookie(default=None),
) -> dict[str, Any]:
    """Run the research pass and propose at most one decision."""
    _require_user(aitrader_session)
    try:
        budget = Decimal(payload.budget) if payload.budget else settings.default_budget_usd
    except InvalidOperation as exc:
        raise HTTPException(status_code=400, detail="budget must be a decimal value") from exc
    if budget <= 0 or budget > settings.max_budget_usd:
        raise HTTPException(status_code=400, detail=f"budget must be > 0 and <= {settings.max_budget_usd}")

    analysis = engine.analyze(
        AnalysisRequest(budget=budget, pool_size=payload.pool_size, weights=SourceWeights(0.35, 0.25, 0.40))
    )
    recommendation = analysis.get("recommendation") or {}
    if recommendation.get("decision") != "buy" or not recommendation.get("symbol"):
        return {"status": "no_trade", "reason": recommendation.get("rationale", "No candidate cleared the bar."), "analysis": analysis}

    amount = Decimal(str(recommendation.get("dollar_amount") or budget))
    amount = min(amount, settings.max_budget_usd)

    try:
        decision = decisions.propose(
            symbol=str(recommendation["symbol"]),
            side="buy",
            amount_usd=amount,
            confidence=float(recommendation.get("confidence") or 0),
            reason=str(recommendation.get("rationale") or "No rationale supplied."),
            ttl_minutes=settings.decision_ttl_minutes,
            policy={"max_budget_usd": str(settings.max_budget_usd), "long_only": True},
        )
    except DecisionClosed as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    delivery: dict[str, Any] = {"sent": False}
    if payload.notify and telegram.configured:
        result = telegram.send_decision(decision)
        delivery = {"sent": bool(result.get("ok")), "detail": result.get("description")}
        if result.get("ok"):
            message = result.get("result") or {}
            decision.delivery = {
                "channel": "telegram",
                "chat_id": (message.get("chat") or {}).get("id"),
                "message_id": message.get("message_id"),
            }
            decision_store.save(decision)
    elif payload.notify:
        delivery = {"sent": False, "detail": "Telegram is not configured."}

    return {"status": "proposed", "decision": decision.to_dict(), "delivery": delivery}


@app.post("/api/telegram/webhook")
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> dict[str, Any]:
    """Telegram callback handler.

    Two independent checks guard this: the shared secret proves the request came
    from Telegram, and the responder id proves it came from the account that owns
    the account being traded.
    """
    secret = settings.telegram_webhook_secret
    if not secret or x_telegram_bot_api_secret_token != secret:
        raise HTTPException(status_code=401, detail="Bad webhook secret.")

    update = await request.json()
    callback = (update or {}).get("callback_query")
    if not isinstance(callback, dict):
        return {"ok": True, "ignored": "not a callback_query"}

    responder = str(((callback.get("from") or {}).get("id")) or "")
    if not settings.telegram_chat_id or responder != str(settings.telegram_chat_id):
        telegram.answer_callback(str(callback.get("id")), "This account cannot answer.", alert=True)
        return {"ok": True, "rejected": "unauthorized responder"}

    parsed = parse_callback(callback.get("data") or "")
    if parsed is None:
        return {"ok": True, "ignored": "unrecognized callback data"}
    decision_id, approved = parsed

    try:
        decision = decisions.answer(decision_id, approved=approved, responder=responder)
    except DecisionClosed as exc:
        telegram.answer_callback(str(callback.get("id")), str(exc), alert=True)
        return {"ok": True, "closed": str(exc)}

    telegram.answer_callback(
        str(callback.get("id")),
        "Approved. Placing the order." if approved else "Skipped. Nothing was placed.",
    )
    message = callback.get("message") or {}
    if message.get("message_id"):
        telegram.settle_message((message.get("chat") or {}).get("id"), message.get("message_id"), decision)

    return {"ok": True, "decision": decision.to_dict()}


class CredentialsPayload(BaseModel):
    email: str
    password: str


@app.get("/api/auth/me")
def auth_me(aitrader_session: str | None = Cookie(default=None)) -> dict[str, Any]:
    user = _current_user(aitrader_session)
    return {"authenticated": user is not None, "user": user.to_public() if user else None}


@app.post("/api/auth/signup")
def auth_signup(payload: CredentialsPayload, response: Response) -> dict[str, Any]:
    try:
        user = users.create(payload.email, payload.password)
    except AccountError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _set_session(response, user.user_id)
    return {"user": user.to_public()}


@app.post("/api/auth/login")
def auth_login(payload: CredentialsPayload, response: Response) -> dict[str, Any]:
    try:
        user = users.authenticate(payload.email, payload.password)
    except AccountError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    _set_session(response, user.user_id)
    return {"user": user.to_public()}


@app.post("/api/auth/demo")
def auth_demo(response: Response) -> dict[str, Any]:
    """Skip sign-in and browse with a demo session."""
    user = demo_user()
    _set_session(response, user.user_id)
    return {"user": user.to_public()}


@app.post("/api/auth/logout")
def auth_logout(response: Response) -> dict[str, Any]:
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@app.get("/api/broker/status")
def broker_status() -> dict[str, Any]:
    return broker.status()


@app.get("/api/portfolio/history")
def portfolio_history(period: str = "1M", aitrader_session: str | None = Cookie(default=None)) -> dict[str, Any]:
    _require_user(aitrader_session)
    if period not in {"1D", "1W", "1M", "3M", "1A"}:
        raise HTTPException(status_code=400, detail="period must be one of 1D, 1W, 1M, 3M, 1A")
    try:
        return broker.portfolio_history(period)
    except BrokerError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/api/orders")
def list_orders(aitrader_session: str | None = Cookie(default=None)) -> dict[str, Any]:
    _require_user(aitrader_session)
    try:
        return {"broker": broker.name, "orders": broker.recent_orders()}
    except BrokerError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
