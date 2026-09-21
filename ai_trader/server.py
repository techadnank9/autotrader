from __future__ import annotations

import secrets
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from fastapi import Cookie, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ai_trader.accounts import AccountError, SessionSigner, UserStore, demo_user
from ai_trader.brokers import AlpacaBroker, BrokerError, NullBroker, broker_for
from ai_trader.credentials import BrokerCredentials, Cipher, CredentialError, FileCredentialStore
from ai_trader.config import Settings
from ai_trader.decisions import Decision, DecisionClosed, DecisionService, DecisionStore
from ai_trader.engine import AnalysisRequest, DEFAULT_UNIVERSE, RecommendationEngine, SourceWeights
from ai_trader.portfolio_engine import ManagePortfolioRequest, PortfolioManagementEngine
from ai_trader.robinhood import RobinhoodTrader
from ai_trader.sia import SIAService
from ai_trader.telegram import TelegramClient, parse_callback
from ai_trader import google_oauth
from ai_trader import robinhood_mcp as rh

STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(title="YOLO STREET", version="0.2.0")
settings = Settings.load()
engine = RecommendationEngine(settings)
trader = RobinhoodTrader(settings)
portfolio_engine = PortfolioManagementEngine(settings, trader=trader)
sia_service = SIAService(settings)
telegram = TelegramClient(settings)


def _execute_decision(decision: Decision) -> dict[str, Any]:
    """The only path from an approved decision to a real order.

    It trades through the broker belonging to the user who owns the decision.
    There is no shared platform account. The decision id doubles as the broker's
    client_order_id, so even a duplicated call cannot place a second order.
    """
    if not decision.user_id:
        return {"status": "blocked", "message": "Decision has no owner, so no account to trade in."}
    user_broker = _broker_for_user(decision.user_id)
    if not user_broker.configured:
        return {"status": "blocked", "message": "No broker connected. Connect a broker in Profile to trade."}
    if getattr(user_broker, "live", False) and not settings.allow_live_trading:
        return {"status": "blocked",
                "message": "This account trades real money, and live trading is not enabled on this platform yet."}
    return user_broker.place_notional_buy(
        decision.symbol, Decimal(decision.amount_usd), client_order_id=decision.decision_id
    )


def _build_stores():
    """Postgres when DATABASE_URL is set, JSON files otherwise."""
    cipher = Cipher(settings.credentials_encryption_key)
    if settings.database_url:
        try:
            from ai_trader.db import Database, PostgresCredentialStore, PostgresDecisionStore, PostgresUserStore

            database = Database(settings.database_url)
            return (PostgresUserStore(database), PostgresDecisionStore(database),
                    PostgresCredentialStore(database, cipher), "postgres")
        except Exception as exc:  # a broken DSN must not take the whole app down
            print(f"Postgres unavailable, falling back to file stores: {exc}")
    return (UserStore(settings.account_dir), DecisionStore(settings.decision_dir),
            FileCredentialStore(settings.account_dir, cipher), "files")


users, decision_store, credential_store, STORAGE_BACKEND = _build_stores()
decisions = DecisionService(decision_store, executor=_execute_decision)
sessions = SessionSigner(settings.session_secret)


def _save_robinhood(user_id: str, client_id: str, refresh_token: str | None, extra: dict[str, Any]) -> None:
    credential_store.save(user_id, BrokerCredentials("robinhood", client_id, refresh_token or "", True, extra))


def _broker_for_user(user_id: str):
    """A user has at most one active broker; connecting one removes the other."""
    if user_id == "demo":
        return NullBroker()
    try:
        rc = credential_store.get(user_id, "robinhood")
        if rc is not None:
            return rh.RobinhoodBroker(
                rc, max_order_usd=settings.max_budget_usd,
                persist=lambda upd: _save_robinhood(user_id, rc.key_id, upd["refresh_token"], upd["extra"]),
            )
        return broker_for(credential_store.get(user_id, "alpaca"), settings)
    except CredentialError:
        return NullBroker()
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
            "credential_storage": Cipher(settings.credentials_encryption_key).ready,
            "live_trading_allowed": settings.allow_live_trading,
            "google_login": bool(settings.google_client_id and settings.google_client_secret),
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
def portfolio_snapshot(aitrader_session: str | None = Cookie(default=None)) -> dict[str, Any]:
    """The signed-in user's own brokerage account, or an empty snapshot.

    There is deliberately no fallback to the local Codex/Robinhood session: that
    is one operator's account, and falling back to it would show every signed-in
    user someone else's brokerage.
    """
    user = _current_user(aitrader_session)
    broker = _broker_for_user(user.user_id) if user else NullBroker()
    meta = {"active_agent": portfolio_engine.registry.list_agents()["current"]}
    if not broker.configured:
        empty = {"portfolio": {}, "positions": [], "warnings": [], "source": "none"}
        return {"account_snapshot": empty, "meta": meta}
    try:
        portfolio = broker.account()
        snapshot = {
            "agentic_account": {"account_number_masked": portfolio.get("account_number_masked")},
            "portfolio": portfolio,
            "positions": broker.positions(),
            "warnings": [],
            "source": broker.name,
        }
    except BrokerError as exc:
        snapshot = {"portfolio": {}, "positions": [], "warnings": [str(exc)], "source": broker.name}
    return {"account_snapshot": snapshot, "meta": meta}


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
            payload.decision_id, approved=payload.approved,
            responder=f"dashboard:{user.user_id}", user_id=user.user_id,
        )
    except DecisionClosed as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"decision": decision.to_dict()}


@app.get("/api/telegram/status")
def telegram_status() -> dict[str, Any]:
    return telegram.status()


@app.get("/api/decisions")
def list_decisions(aitrader_session: str | None = Cookie(default=None)) -> dict[str, Any]:
    user = _require_user(aitrader_session)
    return {
        "open": [d.to_dict() for d in decision_store.open_decisions(user_id=user.user_id)],
        "recent": [d.to_dict() for d in decision_store.list(user_id=user.user_id, limit=50)],
    }


@app.post("/api/decisions/propose")
def propose_decision(
    payload: ProposePayload,
    aitrader_session: str | None = Cookie(default=None),
) -> dict[str, Any]:
    """Run the research pass and propose at most one decision."""
    user = _require_user(aitrader_session)
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
            user_id=user.user_id,
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

    # Telegram is configured with one chat, but decisions now belong to individual
    # users. Sending here would deliver every user's card to that one chat, so
    # delivery waits for per-user Telegram linking rather than leak across users.
    delivery: dict[str, Any] = {
        "sent": False,
        "detail": "Telegram delivery needs per-user linking; approve from the dashboard for now.",
    }

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


class AlpacaConnectPayload(BaseModel):
    key_id: str = Field(min_length=8, max_length=128)
    secret_key: str = Field(min_length=16, max_length=256)
    live: bool = False
    confirm_live: bool = False


@app.get("/api/broker/status")
def broker_status(aitrader_session: str | None = Cookie(default=None)) -> dict[str, Any]:
    user = _require_user(aitrader_session)
    summary = None
    provider = None
    try:
        for prov in ("robinhood", "alpaca"):
            summary = credential_store.summary(user.user_id, prov)
            if summary:
                provider = prov
                break
    except CredentialError:
        pass
    status = _broker_for_user(user.user_id).status()
    return {
        **status,
        "provider": provider,
        "saved": summary,
        "demo": user.is_demo,
        "can_connect": (not user.is_demo) and Cipher(settings.credentials_encryption_key).ready,
        "live_trading_allowed": settings.allow_live_trading,
    }


@app.post("/api/broker/alpaca")
def connect_alpaca(payload: AlpacaConnectPayload, aitrader_session: str | None = Cookie(default=None)) -> dict[str, Any]:
    """Connect the signed-in user's own Alpaca account.

    The keys are checked against Alpaca before anything is stored, and the secret
    is encrypted at rest and never returned.
    """
    user = _require_user(aitrader_session)
    if user.is_demo:
        raise HTTPException(status_code=403, detail="Demo sessions are shared, so they cannot connect a broker. Create an account first.")
    if payload.live and not settings.allow_live_trading:
        raise HTTPException(status_code=403, detail="Live trading is not enabled on this platform. Connect a paper account.")
    if payload.live and not payload.confirm_live:
        raise HTTPException(status_code=400, detail="Confirm that approved orders will use real money.")

    key_id, secret = payload.key_id.strip(), payload.secret_key.strip()
    try:
        account = AlpacaBroker(key_id, secret, live=payload.live, max_order_usd=settings.max_budget_usd).verify()
    except BrokerError as exc:
        hint = " These look like paper keys; choose Paper." if (payload.live and key_id.startswith("PK")) else ""
        raise HTTPException(status_code=400, detail=f"{exc}{hint}") from exc

    try:
        credential_store.save(user.user_id, BrokerCredentials("alpaca", key_id, secret, payload.live))
        credential_store.delete(user.user_id, "robinhood")  # one active broker
    except CredentialError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"connected": True, "mode": "live" if payload.live else "paper", "account": account,
            "saved": credential_store.summary(user.user_id, "alpaca")}


@app.delete("/api/broker/alpaca")
def disconnect_alpaca(aitrader_session: str | None = Cookie(default=None)) -> dict[str, Any]:
    user = _require_user(aitrader_session)
    try:
        removed = credential_store.delete(user.user_id, "alpaca")
    except CredentialError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"disconnected": removed}


@app.get("/api/portfolio/history")
def portfolio_history(period: str = "1M", aitrader_session: str | None = Cookie(default=None)) -> dict[str, Any]:
    user = _require_user(aitrader_session)
    if period not in {"1D", "1W", "1M", "3M", "1A"}:
        raise HTTPException(status_code=400, detail="period must be one of 1D, 1W, 1M, 3M, 1A")
    try:
        return _broker_for_user(user.user_id).portfolio_history(period)
    except BrokerError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/api/orders")
def list_orders(aitrader_session: str | None = Cookie(default=None)) -> dict[str, Any]:
    user = _require_user(aitrader_session)
    broker = _broker_for_user(user.user_id)
    try:
        return {"broker": broker.name, "orders": broker.recent_orders()}
    except BrokerError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


OAUTH_COOKIE = "aitrader_oauth"


def _google_redirect_uri(request: Request) -> str:
    base = settings.public_base_url or str(request.base_url).rstrip("/")
    return f"{base}/auth/google/callback"


def _login_error(message: str) -> RedirectResponse:
    from urllib.parse import quote

    return RedirectResponse(f"/login?error={quote(message)}", status_code=303)


@app.get("/auth/google")
def google_start(request: Request):
    if not (settings.google_client_id and settings.google_client_secret):
        return _login_error("Google sign-in is not configured yet.")
    state, verifier, challenge = google_oauth.new_flow()
    response = RedirectResponse(
        google_oauth.authorize_url(settings.google_client_id, _google_redirect_uri(request), state, challenge),
        status_code=303,
    )
    # state + PKCE verifier, signed so they cannot be forged, valid for 10 minutes.
    response.set_cookie(
        OAUTH_COOKIE, sessions.issue(f"{state}.{verifier}", ttl=600),
        max_age=600, httponly=True, samesite="lax", secure=settings.session_secure_cookie, path="/auth",
    )
    return response


@app.get("/auth/google/callback")
def google_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    aitrader_oauth: str | None = Cookie(default=None),
):
    if error:
        return _login_error("Google sign-in was cancelled.")
    held = sessions.verify(aitrader_oauth)
    if not held or not code or not state or "." not in held:
        return _login_error("Sign-in expired. Please try again.")
    expected_state, verifier = held.split(".", 1)
    if not secrets.compare_digest(expected_state, state):
        return _login_error("Sign-in could not be verified. Please try again.")
    try:
        profile = google_oauth.exchange(
            settings.google_client_id, settings.google_client_secret,
            _google_redirect_uri(request), code, verifier,
        )
    except google_oauth.GoogleAuthError as exc:
        return _login_error(str(exc))

    user = users.get_or_create_verified(profile["email"], "google")
    response = RedirectResponse("/app", status_code=303)
    _set_session(response, user.user_id)
    response.delete_cookie(OAUTH_COOKIE, path="/auth")
    return response


RH_COOKIE = "aitrader_rh"


def _robinhood_redirect_uri(request: Request) -> str:
    base = settings.public_base_url or str(request.base_url).rstrip("/")
    return f"{base}/auth/robinhood/callback"


def _profile_notice(message: str, ok: bool = False) -> RedirectResponse:
    from urllib.parse import quote

    return RedirectResponse(f"/profile?{'ok' if ok else 'error'}={quote(message)}", status_code=303)


@app.get("/auth/robinhood")
def robinhood_start(request: Request, aitrader_session: str | None = Cookie(default=None)):
    """Send the signed-in user to Robinhood to authorize their own Agentic account."""
    user = _current_user(aitrader_session)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    if user.is_demo:
        return _profile_notice("Demo sessions are shared, so they cannot connect a broker. Create an account first.")
    if not Cipher(settings.credentials_encryption_key).ready:
        return _profile_notice("Broker connections are not enabled on this server yet.")
    redirect_uri = _robinhood_redirect_uri(request)
    try:
        meta = rh.discover()
        client_id = rh.register_client(meta, redirect_uri)
    except rh.RobinhoodError as exc:
        return _profile_notice(str(exc))
    state, verifier, challenge = google_oauth.new_flow()
    response = RedirectResponse(rh.authorize_url(meta, client_id, redirect_uri, state, challenge), status_code=303)
    response.set_cookie(
        RH_COOKIE, sessions.issue(f"{user.user_id}|{state}|{verifier}|{client_id}", ttl=900),
        max_age=900, httponly=True, samesite="lax", secure=settings.session_secure_cookie, path="/auth",
    )
    return response


@app.get("/auth/robinhood/callback")
def robinhood_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    aitrader_session: str | None = Cookie(default=None),
    aitrader_rh: str | None = Cookie(default=None),
):
    user = _current_user(aitrader_session)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    if error:
        return _profile_notice("Robinhood connection was cancelled.")
    held = sessions.verify(aitrader_rh)
    parts = held.split("|") if held else []
    if len(parts) != 4 or not code or not state:
        return _profile_notice("Robinhood connection expired. Please try again.")
    owner, expected_state, verifier, client_id = parts
    # The flow must finish in the same account that started it.
    if owner != user.user_id or not secrets.compare_digest(expected_state, state):
        return _profile_notice("Robinhood connection could not be verified. Please try again.")
    try:
        tokens = rh.exchange_code(rh.discover(), client_id, _robinhood_redirect_uri(request), code, verifier)
    except rh.RobinhoodError as exc:
        return _profile_notice(str(exc))

    extra = {"access_token": tokens["access_token"], "expires_at": tokens["expires_at"]}
    try:
        _save_robinhood(user.user_id, client_id, tokens.get("refresh_token"), extra)
        credential_store.delete(user.user_id, "alpaca")  # one active broker
    except CredentialError as exc:
        return _profile_notice(str(exc))

    response = _profile_notice("Robinhood connected.", ok=True)
    response.delete_cookie(RH_COOKIE, path="/auth")
    return response


@app.delete("/api/broker/robinhood")
def disconnect_robinhood(aitrader_session: str | None = Cookie(default=None)) -> dict[str, Any]:
    user = _require_user(aitrader_session)
    try:
        removed = credential_store.delete(user.user_id, "robinhood")
    except CredentialError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"disconnected": removed}
