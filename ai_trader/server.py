from __future__ import annotations

import secrets
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from fastapi import Cookie, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
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
from ai_trader.telegram import TelegramClient
from ai_trader import alerts
from ai_trader import google_oauth
from ai_trader import robinhood_mcp as rh
from ai_trader.picks import FilePicksStore, PicksService
from ai_trader import market

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
    spec = decision.policy.get("order") or {}
    if not spec:
        return user_broker.place_notional_buy(
            decision.symbol, Decimal(decision.amount_usd), client_order_id=decision.decision_id
        )
    dec = lambda k: Decimal(spec[k]) if spec.get(k) not in (None, "") else None  # noqa: E731
    return user_broker.place_buy(
        decision.symbol, client_order_id=decision.decision_id,
        notional=dec("notional"), qty=dec("qty"),
        take_profit=dec("take_profit"), stop_loss=dec("stop_loss"), est_price=dec("est_price"),
        limit_price=dec("limit_price"), good_until=spec.get("good_until") or "day",
    )


def _build_stores():
    """Postgres when DATABASE_URL is set, JSON files otherwise."""
    cipher = Cipher(settings.credentials_encryption_key)
    if settings.database_url:
        try:
            from ai_trader.db import (Database, PostgresCredentialStore, PostgresDecisionStore,
                                      PostgresPicksStore, PostgresUserStore)

            database = Database(settings.database_url)
            return (PostgresUserStore(database), PostgresDecisionStore(database),
                    PostgresCredentialStore(database, cipher), PostgresPicksStore(database),
                    alerts.PostgresLinkStore(database), alerts.PostgresLinkStore(database, "imessage_links"),
                    "postgres")
        except Exception as exc:  # a broken DSN must not take the whole app down
            print(f"Postgres unavailable, falling back to file stores: {exc}")
    return (UserStore(settings.account_dir), DecisionStore(settings.decision_dir),
            FileCredentialStore(settings.account_dir, cipher), FilePicksStore(settings.replay_log_dir),
            alerts.FileLinkStore(settings.account_dir), alerts.FileLinkStore(settings.account_dir, "imessage_links.json"),
            "files")


users, decision_store, credential_store, picks_store, tg_links, im_links, STORAGE_BACKEND = _build_stores()
picks = PicksService(engine, picks_store, ttl_hours=settings.picks_ttl_hours,
                     budget=min(settings.default_budget_usd, settings.max_budget_usd))
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


def _needs_broker(user) -> bool:
    """A real account must connect a broker before using the dashboard.

    Demo sessions are exempt (they are shared and cannot hold keys), and so is
    everyone when the server cannot store credentials, or nobody could get in.
    """
    if user.is_demo or not Cipher(settings.credentials_encryption_key).ready:
        return False
    try:
        return not any(credential_store.summary(user.user_id, p) for p in ("alpaca", "robinhood"))
    except CredentialError:
        return False


@app.get("/app")
def dashboard(aitrader_session: str | None = Cookie(default=None)):
    user = _current_user(aitrader_session)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    if _needs_broker(user):
        return RedirectResponse("/connect", status_code=303)
    return FileResponse(STATIC_DIR / "dashboard.html")


@app.get("/connect")
def connect_page(aitrader_session: str | None = Cookie(default=None)):
    """Onboarding step after sign-up: connect a brokerage."""
    user = _current_user(aitrader_session)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    if not _needs_broker(user):
        return RedirectResponse("/app", status_code=303)
    return FileResponse(STATIC_DIR / "connect.html")


@app.get("/profile")
def profile_page(aitrader_session: str | None = Cookie(default=None)):
    if _current_user(aitrader_session) is None:
        return RedirectResponse("/login", status_code=303)
    return FileResponse(STATIC_DIR / "profile.html")


@app.get("/portfolio")
def portfolio_page(aitrader_session: str | None = Cookie(default=None)):
    user = _current_user(aitrader_session)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    if _needs_broker(user):
        return RedirectResponse("/connect", status_code=303)
    return FileResponse(STATIC_DIR / "portfolio.html")


@app.get("/alerts")
def alerts_page(aitrader_session: str | None = Cookie(default=None)):
    if _current_user(aitrader_session) is None:
        return RedirectResponse("/login", status_code=303)
    return FileResponse(STATIC_DIR / "alerts.html")


@app.get("/stock/{symbol}")
def stock_page(symbol: str, aitrader_session: str | None = Cookie(default=None)):
    if _current_user(aitrader_session) is None:
        return RedirectResponse("/login", status_code=303)
    return FileResponse(STATIC_DIR / "stockpage.html")


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
            "ranking_model": (getattr(engine.ranker, "model", None) or "claude-opus-5") if engine.ranker.enabled else None,
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


def _base_url(request: Request | None = None) -> str:
    return (settings.public_base_url or (str(request.base_url) if request else "")).rstrip("/")


def _notify(user_id: str, kind: str, text: str, rows: list | None = None) -> None:
    """Best effort: an alert must never break the action that triggered it."""
    if telegram.configured:
        try:
            link = tg_links.get(user_id)
            if link and link["prefs"].get(kind, True):
                telegram.send(link["chat_id"], text, rows)
        except Exception as exc:  # noqa: BLE001
            print(f"telegram notify failed: {type(exc).__name__}")
    if _imsg_ready():
        try:
            link = im_links.get(user_id)
            if link and link["prefs"].get("verified") and link["prefs"].get(kind, True):
                url = next((b["url"] for r in (rows or []) for b in r if b.get("url")), None)
                _imsg_send(link["chat_id"], _plain(text) + (f"\n{url}" if url else ""))
        except Exception as exc:  # noqa: BLE001
            print(f"imessage notify failed: {type(exc).__name__}")


def _send_digest(link: dict[str, Any], run: dict[str, Any]) -> dict[str, Any]:
    text, rows = alerts.picks_digest(run, _base_url(), settings.default_budget_usd)
    return telegram.send(link["chat_id"], text, rows)


def _broadcast_picks(run: dict[str, Any]) -> int:
    if run.get("status") != "ok":
        return 0
    sent = 0
    if telegram.configured:
        for link in tg_links.all():
            if link["prefs"].get("picks", True) and _send_digest(link, run).get("ok"):
                sent += 1
    if _imsg_ready():
        for link in im_links.all():
            if link["prefs"].get("verified") and link["prefs"].get("picks", True):
                try:
                    _imsg_digest(link, run)
                    sent += 1
                except Exception as exc:  # noqa: BLE001
                    print(f"imessage digest failed: {type(exc).__name__}")
    return sent


class TelegramPrefs(BaseModel):
    picks: bool | None = None
    orders: bool | None = None


def _tg_view(user) -> dict[str, Any]:
    link = tg_links.get(user.user_id)
    return {
        "available": telegram.configured and not user.is_demo,
        "is_demo": user.is_demo,
        "connected": bool(link),
        "username": (link or {}).get("username"),
        "name": (link or {}).get("name"),
        "linked_at": (link or {}).get("linked_at"),
        "prefs": (link or {}).get("prefs") or dict(alerts.DEFAULT_PREFS),
        "bot": telegram.bot_username() if telegram.configured else None,
    }


@app.get("/api/telegram")
def telegram_me(aitrader_session: str | None = Cookie(default=None)) -> dict[str, Any]:
    return _tg_view(_require_user(aitrader_session))


@app.post("/api/telegram/link")
def telegram_link(request: Request, aitrader_session: str | None = Cookie(default=None)) -> dict[str, Any]:
    """A one-time link that binds whichever Telegram chat opens it to this account."""
    user = _require_user(aitrader_session)
    if user.is_demo:
        raise HTTPException(status_code=403, detail="Create an account to get alerts.")
    if not telegram.configured:
        raise HTTPException(status_code=503, detail="Telegram alerts aren't available right now.")
    telegram.ensure_setup(f"{_base_url(request)}/api/telegram/webhook", alerts.webhook_secret(settings))
    bot = telegram.bot_username()
    if not bot:
        raise HTTPException(status_code=503, detail="Telegram isn't reachable right now. Try again in a minute.")
    code = alerts.make_link_code(settings.session_secret, user.user_id)
    url = f"https://t.me/{bot}?start={code}"
    qr = None
    try:
        import segno
        qr = segno.make(url, error="m").svg_inline(scale=5, border=2, dark="#08090B", light="#FFFFFF")
    except Exception:  # noqa: BLE001  the link alone still works
        pass
    return {"url": url, "bot": bot, "qr_svg": qr, "expires_in": alerts.LINK_TTL_SECONDS}


@app.patch("/api/telegram/prefs")
def telegram_prefs(payload: TelegramPrefs, aitrader_session: str | None = Cookie(default=None)) -> dict[str, Any]:
    user = _require_user(aitrader_session)
    changes = {k: v for k, v in payload.model_dump().items() if v is not None}
    if not tg_links.set_prefs(user.user_id, changes):
        raise HTTPException(status_code=404, detail="Telegram isn't connected.")
    return _tg_view(user)


@app.post("/api/telegram/test")
def telegram_test(aitrader_session: str | None = Cookie(default=None)) -> dict[str, Any]:
    user = _require_user(aitrader_session)
    link = tg_links.get(user.user_id)
    if not link:
        raise HTTPException(status_code=404, detail="Telegram isn't connected.")
    run = picks_store.latest()
    r = _send_digest(link, run) if run and run.get("status") == "ok" else telegram.send(
        link["chat_id"], "✅ <b>Alerts are working.</b>\nToday's picks will arrive here each weekday morning.")
    if not r.get("ok"):
        raise HTTPException(status_code=502, detail="Telegram didn't accept the message. If you blocked the bot, "
                                                    "unblock it or connect again.")
    return {"sent": True}


@app.delete("/api/telegram/link")
def telegram_unlink(aitrader_session: str | None = Cookie(default=None)) -> dict[str, Any]:
    user = _require_user(aitrader_session)
    link = tg_links.get(user.user_id)
    removed = tg_links.unlink(user.user_id)
    if link and telegram.configured:
        telegram.send(link["chat_id"], "Disconnected from AI Trader. You won't get alerts here anymore.")
    return {"disconnected": removed}


def _tg_reply(chat_id: Any, text: str) -> None:
    telegram.send(chat_id, text)


def _tg_message(msg: dict[str, Any]) -> None:
    chat = msg.get("chat") or {}
    if chat.get("type") != "private":
        return
    chat_id, sender = chat.get("id"), msg.get("from") or {}
    text = (msg.get("text") or "").strip()
    cmd, _, arg = text.partition(" ")
    cmd = cmd.split("@")[0].lower()
    link = tg_links.by_chat(str(chat_id))
    connect = f"To connect, open {_base_url()}/alerts and tap <b>Connect Telegram</b>."

    if cmd == "/start" and arg.strip():
        uid = alerts.read_link_code(settings.session_secret, arg.strip())
        user = users.get(uid) if uid else None
        if not user or user.is_demo:
            _tg_reply(chat_id, "That link has expired. Open the Alerts page in the app and tap "
                               "<b>Connect Telegram</b> again.")
            return
        tg_links.link(user.user_id, str(chat_id), sender.get("username"), sender.get("first_name"))
        _tg_reply(chat_id, "✅ <b>Connected to AI Trader</b>\n\n"
                           "You'll get today's picks each weekday around 9 AM ET, with a Buy button, and a "
                           "message whenever one of your orders is placed or canceled.\n\n"
                           "Send /picks any time for today's picks. Send /stop to disconnect.")
        return
    if cmd == "/picks":
        if not link:
            _tg_reply(chat_id, connect)
            return
        run = picks_store.latest()
        if run and run.get("status") == "ok":
            _send_digest(link, run)
        else:
            _tg_reply(chat_id, "Today's picks aren't ready yet. They arrive each weekday around 9 AM ET.")
        return
    if cmd == "/stop":
        if link:
            tg_links.unlink(link["user_id"])
            _tg_reply(chat_id, f"Disconnected. You won't get alerts here anymore. Reconnect any time at "
                               f"{_base_url()}/alerts.")
        else:
            _tg_reply(chat_id, "This chat isn't connected to an account.")
        return
    if link:
        _tg_reply(chat_id, "You're connected. Today's picks arrive each weekday around 9 AM ET.\n"
                           "/picks · today's picks\n/stop · disconnect")
    else:
        _tg_reply(chat_id, "This bot sends your AI Trader picks and order updates.\n\n" + connect)


def _tg_callback(cb: dict[str, Any]) -> None:
    from time import sleep

    cb_id = str(cb.get("id"))
    msg = cb.get("message") or {}
    chat_id, message_id = (msg.get("chat") or {}).get("id"), msg.get("message_id")
    link = tg_links.by_chat(str((cb.get("from") or {}).get("id")))
    if not link or str(chat_id) != link["chat_id"]:
        telegram.answer_callback(cb_id, "This chat isn't connected to an account.", alert=True)
        return
    user = users.get(link["user_id"])
    if not user:
        telegram.answer_callback(cb_id, "Account not found.", alert=True)
        return
    data = str(cb.get("data") or "")
    action, _, rest = data.partition(":")
    amount = settings.default_budget_usd

    if action == "px":
        telegram.answer_callback(cb_id, "Canceled.")
        telegram.edit(chat_id, message_id, "Canceled. Nothing was bought.")
        return

    run = picks_store.latest()
    if action == "pb":
        symbol = rest.upper()
        pick = next((p for p in (run or {}).get("picks", []) if p["symbol"] == symbol), None)
        if not pick:
            telegram.answer_callback(cb_id, "This pick is out of date. Send /picks for today's.", alert=True)
            return
        if pick.get("verdict") != "buy":
            telegram.answer_callback(cb_id, f"{symbol} isn't a buy today.", alert=True)
            return
        if _needs_broker(user):
            telegram.answer_callback(cb_id, "Connect a broker in the app first.", alert=True)
            return
        try:
            ctx = market.context(symbol)
        except market.MarketDataError:
            telegram.answer_callback(cb_id, "Couldn't get a price right now. Try again.", alert=True)
            return
        broker = _broker_for_user(user.user_id)
        telegram.answer_callback(cb_id, "")
        telegram.send(chat_id, alerts.confirm_text(symbol, amount, ctx["price"],
                                                   "live" if getattr(broker, "live", False) else "paper",
                                                   bool(ctx.get("market_open"))),
                      [[{"text": "Cancel", "callback_data": "px"},
                        {"text": f"Confirm buy", "callback_data": f"pc:{symbol}:{run['run_id']}"[:64]}]])
        return

    if action == "pc":
        symbol, _, run_id = rest.partition(":")
        telegram.answer_callback(cb_id, "Placing your order…")
        telegram.edit(chat_id, message_id, f"Placing your order for <b>{symbol}</b>…")
        try:
            result = _place_order(user, BuyPickPayload(symbol=symbol, amount=str(amount), run_id=run_id))
        except HTTPException as exc:
            telegram.edit(chat_id, message_id, alerts.order_text("failed", symbol, str(exc.detail)))
            return
        ex = result.get("execution") or {}
        if ex.get("status") != "submitted":
            telegram.edit(chat_id, message_id, alerts.order_text("failed", symbol, ex.get("message") or "The broker declined it."))
            return
        text = alerts.order_text("placed", symbol, f"Buy {alerts._money(amount)}. Sent to your broker; "
                                                         "it fills while the market is open (9:30 AM to 4 PM ET).")
        broker = _broker_for_user(user.user_id)
        for _ in range(3):  # market orders usually fill within a couple of seconds
            sleep(1.2)
            try:
                o = broker.get_order(ex["order_id"])
            except Exception:  # noqa: BLE001
                break
            if o.get("status") == "filled":
                text = alerts.order_text("filled", symbol, f"Bought {o.get('filled_qty')} shares at "
                                                           f"${float(o.get('filled_avg_price') or 0):,.2f}.")
                break
        telegram.edit(chat_id, message_id, text,
                      [[{"text": "View in the app", "url": f"{_base_url()}/stock/{symbol}"}]])
        return

    telegram.answer_callback(cb_id, "")


@app.post("/api/telegram/webhook")
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> dict[str, Any]:
    """Messages and button presses from the bot. The secret header proves the request
    came from Telegram; the chat id decides whose account it acts on."""
    if not telegram.configured or x_telegram_bot_api_secret_token != alerts.webhook_secret(settings):
        raise HTTPException(status_code=401, detail="Bad webhook secret.")
    update = await request.json() or {}
    try:
        # handlers do blocking I/O (and market data runs its own event loop), so off the loop they go
        if isinstance(update.get("message"), dict):
            await run_in_threadpool(_tg_message, update["message"])
        elif isinstance(update.get("callback_query"), dict):
            await run_in_threadpool(_tg_callback, update["callback_query"])
    except Exception as exc:  # noqa: BLE001  never make Telegram retry a crash forever
        print(f"telegram webhook error: {type(exc).__name__}: {exc}")
    return {"ok": True}


# -- iMessage (Photon Spectrum) ----------------------------------------------------
#
# A small Node service in /imsg does the actual sending and receiving. This side owns
# who is linked, what a reply means, and every order. A number is linked only after
# the person replies YES from it, which proves the phone is theirs.

import os as _os
import re as _re
import httpx as _httpx

_SPECTRUM = "https://spectrum.photon.codes"
_IMSG_LINE = _os.environ.get("IMSG_LINE", "+1 (415) 605-5508")


def _imsg_ready() -> bool:
    return bool(_os.environ.get("SPECTRUM_PROJECT_ID") and _os.environ.get("SPECTRUM_PROJECT_SECRET")
                and _os.environ.get("IMSG_INTERNAL_SECRET"))


def _plain(html_text: str) -> str:
    import html as _html
    return _html.unescape(_re.sub(r"<[^>]+>", "", html_text))


def _norm_phone(raw: str) -> str | None:
    raw = (raw or "").strip()
    digits = _re.sub(r"\D", "", raw)
    if raw.startswith("+") and 8 <= len(digits) <= 15:
        return "+" + digits
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    return None


def _mask(phone: str) -> str:
    d = _re.sub(r"\D", "", phone or "")
    return f"(•••) •••-{d[-4:]}" if len(d) >= 4 else "••••"


def _pretty(phone: str | None) -> str | None:
    d = _re.sub(r"\D", "", phone or "")
    if len(d) == 11 and d.startswith("1"):
        return f"+1 ({d[1:4]}) {d[4:7]}-{d[7:]}"
    return phone


def _photon(method: str, path: str, body: dict | None = None) -> dict[str, Any]:
    pid = _os.environ["SPECTRUM_PROJECT_ID"]
    r = _httpx.request(method, f"{_SPECTRUM}/projects/{pid}{path}", json=body, timeout=15,
                       auth=(pid, _os.environ["SPECTRUM_PROJECT_SECRET"]))
    try:
        return r.json()
    except ValueError:
        return {"succeed": False, "status": r.status_code}


def _photon_user(phone: str, email: str) -> dict[str, Any]:
    """Register the number with the project so the shared line is allowed to text it."""
    users_ = ((_photon("GET", "/users").get("data") or {}).get("users")) or []
    hit = next((u for u in users_ if u.get("phoneNumber") == phone), None)
    if hit:
        return hit
    r = _photon("POST", "/users/", {"type": "shared", "phoneNumber": phone, "email": email or None})
    if not r.get("succeed"):
        raise HTTPException(status_code=503, detail="Text alerts are full right now. Use Telegram for now.")
    return r["data"]


def _imsg_send(phone: str, text: str | None = None, poll: dict | None = None) -> None:
    r = _httpx.post(f"{_base_url()}/imsg/send", timeout=30,
                    headers={"x-internal-secret": _os.environ["IMSG_INTERNAL_SECRET"]},
                    json={"phone": phone, "text": text, "poll": poll})
    if r.status_code != 200:
        detail = ""
        try:
            detail = r.json().get("error") or ""
        except ValueError:
            pass
        raise RuntimeError(detail or f"send failed ({r.status_code})")


def _imsg_digest(link: dict[str, Any], run: dict[str, Any]) -> None:
    text, poll, syms = alerts.imessage_digest(run, _base_url(), settings.default_budget_usd)
    im_links.set_prefs(link["user_id"], {"pending": {"run_id": run["run_id"], "symbols": syms,
                                                     "exp": run.get("created_at", 0) + 8 * 3600} if syms else None})
    _imsg_send(link["chat_id"], text, poll)


def _imsg_view(user) -> dict[str, Any]:
    link = im_links.get(user.user_id)
    prefs = (link or {}).get("prefs") or {}
    verified = bool(link and prefs.get("verified"))
    waiting = bool(link and not verified)
    handle = link["chat_id"] if link else ""
    return {
        "available": _imsg_ready() and not user.is_demo,
        "is_demo": user.is_demo,
        "connected": verified,
        "waiting": waiting,
        "phone": _mask(handle) if (verified and not handle.startswith("pending:")) else None,
        "code": prefs.get("code") if waiting else None,
        "line": _IMSG_LINE,
        "line_plain": "+" + _re.sub(r"\D", "", _IMSG_LINE),
        "prefs": {"picks": prefs.get("picks", True), "orders": prefs.get("orders", True)},
    }


_CODE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"


def _new_code() -> str:
    import secrets as _secrets
    return "".join(_secrets.choice(_CODE_ALPHABET) for _ in range(5))


@app.get("/api/imessage")
def imessage_me(aitrader_session: str | None = Cookie(default=None)) -> dict[str, Any]:
    return _imsg_view(_require_user(aitrader_session))


@app.post("/api/imessage/code")
def imessage_code(aitrader_session: str | None = Cookie(default=None)) -> dict[str, Any]:
    """Issue a short code the user texts to our line. Their reply proves the phone
    is theirs and hands us the exact iMessage handle to send to."""
    user = _require_user(aitrader_session)
    if user.is_demo:
        raise HTTPException(status_code=403, detail="Create an account to get alerts.")
    if not _imsg_ready():
        raise HTTPException(status_code=503, detail="Text alerts aren't available right now.")
    existing = im_links.get(user.user_id)
    if existing and existing["prefs"].get("verified"):
        raise HTTPException(status_code=409, detail="Text alerts are already connected.")
    code = _new_code()
    im_links.link(user.user_id, f"pending:{code}", None, None)
    im_links.set_prefs(user.user_id, {"verified": False, "code": code, "code_exp": __import__("time").time() + 1800})
    return _imsg_view(user)


@app.patch("/api/imessage/prefs")
def imessage_prefs(payload: TelegramPrefs, aitrader_session: str | None = Cookie(default=None)) -> dict[str, Any]:
    user = _require_user(aitrader_session)
    changes = {k: v for k, v in payload.model_dump().items() if v is not None}
    if not im_links.set_prefs(user.user_id, changes):
        raise HTTPException(status_code=404, detail="Text alerts aren't connected.")
    return _imsg_view(user)


@app.post("/api/imessage/test")
def imessage_test(aitrader_session: str | None = Cookie(default=None)) -> dict[str, Any]:
    user = _require_user(aitrader_session)
    link = im_links.get(user.user_id)
    if not link or not link["prefs"].get("verified"):
        raise HTTPException(status_code=404, detail="Text alerts aren't connected.")
    run = picks_store.latest()
    try:
        if run and run.get("status") == "ok":
            _imsg_digest(link, run)
        else:
            _imsg_send(link["chat_id"], "Alerts are working. Today's picks will arrive here each weekday morning.")
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail="The text didn't go through. Try again in a minute.") from exc
    return {"sent": True}


@app.delete("/api/imessage")
def imessage_unlink(aitrader_session: str | None = Cookie(default=None)) -> dict[str, Any]:
    user = _require_user(aitrader_session)
    link = im_links.get(user.user_id)
    removed = im_links.unlink(user.user_id)
    if link and link["prefs"].get("verified") and _imsg_ready():
        try:
            _imsg_send(link["chat_id"], "AI Trader alerts are off. Reconnect any time from the Alerts page.")
        except RuntimeError:
            pass
    return {"disconnected": removed}


class InboundText(BaseModel):
    message_id: str | None = None
    phone: str = ""
    text: str | None = None
    vote: str | None = None


_seen_inbound: dict[str, float] = {}


def _order_reply(user, symbol: str, run_id: str) -> str:
    from time import sleep
    amount = settings.default_budget_usd
    try:
        result = _place_order(user, BuyPickPayload(symbol=symbol, amount=str(amount), run_id=run_id))
    except HTTPException as exc:
        return f"Not placed: {exc.detail}"
    ex = result.get("execution") or {}
    if ex.get("status") != "submitted":
        return f"Not placed: {ex.get('message') or 'the broker declined it.'}"
    broker = _broker_for_user(user.user_id)
    for _ in range(3):
        sleep(1.2)
        try:
            o = broker.get_order(ex["order_id"])
        except Exception:  # noqa: BLE001
            break
        if o.get("status") == "filled":
            return (f"Bought {o.get('filled_qty')} shares of {symbol} at "
                    f"${float(o.get('filled_avg_price') or 0):,.2f}. {_base_url()}/stock/{symbol}")
    return (f"Order sent: buy {alerts._money(amount)} of {symbol}. It fills while the market is open "
            f"(9:30 AM to 4 PM ET). {_base_url()}/stock/{symbol}")


@app.post("/api/imessage/inbound")
def imessage_inbound(payload: InboundText, x_internal_secret: str | None = Header(default=None)) -> dict[str, Any]:
    """A text or poll vote from Photon, relayed by /imsg. Returns what to send back."""
    if not _imsg_ready() or x_internal_secret != _os.environ.get("IMSG_INTERNAL_SECRET"):
        raise HTTPException(status_code=401, detail="Unauthorized.")
    now = __import__("time").time()
    if payload.message_id:
        if payload.message_id in _seen_inbound:
            return {}
        _seen_inbound[payload.message_id] = now
    phone = _norm_phone(payload.phone) or payload.phone
    raw = (payload.vote or payload.text or "").strip()
    t = _re.sub(r"[^A-Z. ]", "", raw.upper()).strip()
    t = _re.sub(r"^BUY\s+", "", t)
    print(f"imessage inbound from {_mask(phone)}: {t[:20]!r}")
    link = im_links.by_chat(phone)
    connect = f"To connect, open {_base_url()}/alerts and tap Text to connect."
    if not link:
        # An unrecognised handle: maybe it carries a pending connect code.
        code = _re.sub(r"[^A-Z0-9]", "", (payload.text or "").upper())
        pending = im_links.by_chat(f"pending:{code}") if len(code) == 5 else None
        if pending and (pending["prefs"].get("code_exp", 0) > now):
            u = users.get(pending["user_id"])
            if u:
                im_links.link(u.user_id, phone, None, _IMSG_LINE)   # bind the real handle Apple uses
                im_links.set_prefs(u.user_id, {"verified": True, "code": None})
                try:  # allow-list the handle so the morning picks can be initiated
                    _photon_user(phone, u.email)
                except Exception:  # noqa: BLE001
                    pass
                return {"text": "Connected. You'll get today's picks here each weekday around 9 AM ET, and a text "
                                "when an order is placed or canceled. Reply PICKS any time, STOP to turn alerts off."}
        return {"text": connect}
    prefs = link["prefs"]
    user = users.get(link["user_id"])
    if not user:
        return {"text": connect}

    if t in {"STOP", "UNSUBSCRIBE", "CANCEL", "END", "QUIT"}:
        im_links.unlink(user.user_id)
        return {"text": "Alerts are off. Reconnect any time from the Alerts page in the app."}

    if t in {"PICKS", "PICK", "TODAY"}:
        run = picks_store.latest()
        if not run or run.get("status") != "ok":
            return {"text": "Today's picks aren't ready yet. They arrive each weekday around 9 AM ET."}
        text, poll, syms = alerts.imessage_digest(run, _base_url(), settings.default_budget_usd)
        im_links.set_prefs(user.user_id, {"pending": {"run_id": run["run_id"], "symbols": syms,
                                                      "exp": now + 8 * 3600} if syms else None})
        return {"text": text, "poll": poll}
    if t in {"HELP", "INFO", "?"}:
        return {"text": "AI Trader alerts. Reply PICKS for today's picks, a symbol like META to buy it when it's "
                        "a pick, NO to skip, STOP to turn alerts off."}

    pending = prefs.get("pending") or {}
    syms = (pending.get("symbols") or []) if pending.get("exp", 0) > now else []
    if t in {"NO", "N", "SKIP", "SKIP TODAY", "PASS"}:
        im_links.set_prefs(user.user_id, {"pending": None})
        return {"text": "Skipped. Nothing was bought."}
    choice = None
    if t in syms:
        choice = t
    elif t in {"YES", "Y", "OK", "BUY"}:
        if len(syms) == 1:
            choice = syms[0]
        elif syms:
            return {"text": f"Which one? Reply {' or '.join(syms)}."}
    if choice:
        if _needs_broker(user):
            return {"text": f"Connect a broker first: {_base_url()}/connect"}
        left = [x for x in syms if x != choice]
        im_links.set_prefs(user.user_id, {"pending": {**pending, "symbols": left} if left else None})
        return {"text": _order_reply(user, choice, pending["run_id"])}
    if t in {"YES", "Y"} or (len(t) <= 5 and t.isalpha() and t not in {"HI", "HEY", "HELLO", "THANKS"}):
        return {"text": "There's nothing to buy from that right now. Reply PICKS for today's picks."}
    return {"text": "Reply PICKS for today's picks, or STOP to turn alerts off."}


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


RETURN_PATHS = {"/profile", "/connect"}


def _profile_notice(message: str, ok: bool = False, path: str = "/profile") -> RedirectResponse:
    from urllib.parse import quote

    path = path if path in RETURN_PATHS else "/profile"
    return RedirectResponse(f"{path}?{'ok' if ok else 'error'}={quote(message)}", status_code=303)


@app.get("/auth/robinhood")
def robinhood_start(
    request: Request, return_to: str = "/profile", aitrader_session: str | None = Cookie(default=None)
):
    """Send the signed-in user to Robinhood to authorize their own Agentic account."""
    back = return_to if return_to in RETURN_PATHS else "/profile"
    user = _current_user(aitrader_session)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    if user.is_demo:
        return _profile_notice("Demo sessions are shared, so they cannot connect a broker. Create an account first.", path=back)
    if not Cipher(settings.credentials_encryption_key).ready:
        return _profile_notice("Broker connections are not enabled on this server yet.", path=back)
    redirect_uri = _robinhood_redirect_uri(request)
    try:
        meta = rh.discover()
        client_id = rh.register_client(meta, redirect_uri)
    except rh.RobinhoodError as exc:
        return _profile_notice(str(exc), path=back)
    state, verifier, challenge = google_oauth.new_flow()
    response = RedirectResponse(rh.authorize_url(meta, client_id, redirect_uri, state, challenge), status_code=303)
    response.set_cookie(
        RH_COOKIE, sessions.issue(f"{user.user_id}|{state}|{verifier}|{client_id}|{back}", ttl=900),
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
    held = sessions.verify(aitrader_rh)
    parts = held.split("|") if held else []
    back = parts[4] if len(parts) == 5 and parts[4] in RETURN_PATHS else "/profile"
    if error:
        return _profile_notice("Robinhood connection was cancelled.", path=back)
    if len(parts) != 5 or not code or not state:
        return _profile_notice("Robinhood connection expired. Please try again.", path=back)
    owner, expected_state, verifier, client_id, _ = parts
    # The flow must finish in the same account that started it.
    if owner != user.user_id or not secrets.compare_digest(expected_state, state):
        return _profile_notice("Robinhood connection could not be verified. Please try again.", path=back)
    try:
        tokens = rh.exchange_code(rh.discover(), client_id, _robinhood_redirect_uri(request), code, verifier)
    except rh.RobinhoodError as exc:
        return _profile_notice(str(exc), path=back)

    extra = {"access_token": tokens["access_token"], "expires_at": tokens["expires_at"]}
    try:
        _save_robinhood(user.user_id, client_id, tokens.get("refresh_token"), extra)
        credential_store.delete(user.user_id, "alpaca")  # one active broker
    except CredentialError as exc:
        return _profile_notice(str(exc), path=back)

    response = (RedirectResponse("/app?welcome=robinhood", status_code=303) if back == "/connect"
                else _profile_notice("Robinhood connected.", ok=True))
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


def _bought_from(run_id: str, user_id: str) -> list[str]:
    """Symbols with an order that actually went through. A blocked or rejected
    attempt placed nothing, so it must not stop the user from trying again."""
    return sorted({d.symbol for d in decision_store.list(user_id=user_id, limit=100)
                   if d.policy.get("run_id") == run_id and d.status == "approved"
                   and (d.execution or {}).get("status") == "submitted"})


def _picks_view(run: dict[str, Any], user) -> dict[str, Any]:
    return {
        "run_id": run["run_id"],
        "updated_at": run["created_at"],
        "status": run.get("status"),
        "message": run.get("message"),
        "articles_read": run.get("articles_read", 0),
        "default_amount": f"{settings.default_budget_usd:.2f}",
        "picks": run.get("picks", []),
        "bought": _bought_from(run["run_id"], user.user_id),
        "has_broker": not _needs_broker(user) and not user.is_demo,
        "is_demo": user.is_demo,
    }


@app.get("/api/picks")
def get_picks(aitrader_session: str | None = Cookie(default=None)) -> dict[str, Any]:
    """Today's picks. Served from cache; researched on the spot only when stale."""
    user = _require_user(aitrader_session)
    return _picks_view(picks.latest_or_run(), user)


@app.post("/api/picks/refresh")
def refresh_picks(aitrader_session: str | None = Cookie(default=None)) -> dict[str, Any]:
    user = _require_user(aitrader_session)
    return _picks_view(picks.refresh(), user)


class OrderPayload(BaseModel):
    symbol: str
    mode: str = Field(default="dollars", pattern="^(dollars|shares)$")
    amount: str
    order_type: str = Field(default="market", pattern="^(market|limit)$")
    limit_price: str | None = None
    good_until: str = Field(default="day", pattern="^(day|gtc)$")
    take_profit_pct: float | None = Field(default=None, gt=0, le=1000)
    stop_loss_pct: float | None = Field(default=None, gt=0, lt=100)
    run_id: str | None = None


class BuyPickPayload(OrderPayload):
    run_id: str


def _alpaca_data_for(user):
    """The user's own Alpaca keys, for market data, when they connected Alpaca."""
    if user.is_demo:
        return None
    try:
        c = credential_store.get(user.user_id, "alpaca")
    except CredentialError:
        return None
    return market.AlpacaData(c.key_id, c.secret_key) if c else None


def _dec(raw: str | None, what: str) -> Decimal:
    try:
        v = Decimal(str(raw).strip().replace(",", "").lstrip("$"))
    except (InvalidOperation, AttributeError):
        raise HTTPException(status_code=400, detail=f"Enter a number for {what}.")
    if v <= 0:
        raise HTTPException(status_code=400, detail=f"Enter {what} greater than zero.")
    return v


def _place_order(user, payload: OrderPayload) -> dict[str, Any]:
    """Every buy, from a pick or from search, goes through here and the decision gate."""
    from decimal import ROUND_DOWN

    if user.is_demo:
        raise HTTPException(status_code=403, detail="Create an account and connect a broker to buy.")
    if _needs_broker(user):
        raise HTTPException(status_code=400, detail="Connect a broker to buy.")
    symbol = payload.symbol.upper().strip()
    if not symbol.replace(".", "").isalnum() or len(symbol) > 8:
        raise HTTPException(status_code=400, detail="That is not a stock symbol.")

    pick, run = None, None
    if payload.run_id:
        run = picks_store.latest()
        if not run or run["run_id"] != payload.run_id:
            raise HTTPException(status_code=409, detail="These picks have been updated. Refresh to see the latest.")
        pick = next((p for p in run.get("picks", []) if p["symbol"] == symbol), None)
        if symbol in _bought_from(run["run_id"], user.user_id):
            raise HTTPException(status_code=409, detail=f"You already bought {symbol} from today's picks.")

    try:
        ctx = market.context(symbol)
    except market.MarketDataError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    price = Decimal(str(ctx["price"]))
    if price < 1:
        raise HTTPException(status_code=400, detail=f"{symbol} trades under $1. Penny stocks aren't available here.")
    if market.is_leveraged_or_inverse(ctx.get("name")):
        raise HTTPException(status_code=400, detail=f"{symbol} is a leveraged or inverse fund, which isn't available here.")

    amount = _dec(payload.amount, "an amount")
    limit = _dec(payload.limit_price, "a price") if payload.order_type == "limit" else None
    entry = limit or price
    exit_plan = payload.take_profit_pct is not None or payload.stop_loss_pct is not None
    stays_open = payload.good_until == "gtc" or exit_plan  # exit legs must outlive the day
    notional = qty = None
    note = None

    if payload.mode == "dollars":
        if limit is None and not exit_plan:
            notional = amount.quantize(Decimal("0.01"))
        elif limit is not None and not stays_open:
            qty = (amount / entry).quantize(Decimal("0.0001"), rounding=ROUND_DOWN)
            if qty <= 0:
                raise HTTPException(status_code=400, detail="That amount buys less than a sliver of a share.")
        else:
            qty = (amount / entry).to_integral_value(rounding=ROUND_DOWN)
            why = "the exit plan can be attached" if exit_plan else "the order can stay open"
            if qty < 1:
                raise HTTPException(status_code=400, detail=(
                    f"This needs at least one whole share (about ${entry:,.2f}) so {why}. Raise the amount."))
            note = f"Buying {qty} whole share{'s' if qty != 1 else ''} so {why}."
    else:
        qty = amount
        if stays_open and qty != qty.to_integral_value():
            raise HTTPException(status_code=400, detail=(
                "An exit plan needs a whole number of shares." if exit_plan
                else "An order that stays open needs a whole number of shares."))

    tp = (entry * (1 + Decimal(str(payload.take_profit_pct)) / 100)).quantize(Decimal("0.01")) \
        if payload.take_profit_pct is not None else None
    sl = (entry * (1 - Decimal(str(payload.stop_loss_pct)) / 100)).quantize(Decimal("0.01")) \
        if payload.stop_loss_pct is not None else None
    estimate = notional if notional is not None else (qty * entry).quantize(Decimal("0.01"))
    if estimate > settings.max_budget_usd:
        raise HTTPException(status_code=400, detail=f"Above the ${settings.max_budget_usd:,.2f} per-order limit.")

    order = {"notional": str(notional) if notional is not None else None,
             "qty": str(qty) if qty is not None else None,
             "limit_price": str(limit) if limit is not None else None,
             "good_until": "gtc" if stays_open and notional is None else "day",
             "take_profit": str(tp) if tp is not None else None,
             "stop_loss": str(sl) if sl is not None else None,
             "est_price": str(price)}
    decision = decisions.propose(
        user_id=user.user_id, enforce_open_limit=False,
        symbol=symbol, side="buy", amount_usd=estimate,
        confidence=float((pick or {}).get("score") or 0),
        reason=(pick or {}).get("summary") or "Bought on the user's own judgment.",
        ttl_minutes=settings.decision_ttl_minutes,
        policy={"long_only": True, "run_id": run["run_id"] if run else None,
                "source": "pick" if pick else "search", "order": order},
    )
    decision = decisions.answer(decision.decision_id, approved=True,
                                responder=f"user:{user.user_id}", user_id=user.user_id)
    return {"decision": decision.to_dict(), "execution": decision.execution or {}, "note": note,
            "order": {**order, "estimate": str(estimate)}}


@app.post("/api/picks/buy")
def buy_pick(payload: BuyPickPayload, aitrader_session: str | None = Cookie(default=None)) -> dict[str, Any]:
    user = _require_user(aitrader_session)
    return _order_alert(user, _place_order(user, payload), payload.symbol)


@app.post("/api/orders")
def place_order(payload: OrderPayload, aitrader_session: str | None = Cookie(default=None)) -> dict[str, Any]:
    """Buy any US stock, not only today's picks: the user's own call."""
    user = _require_user(aitrader_session)
    return _order_alert(user, _place_order(user, payload), payload.symbol)


def _order_alert(user, result: dict[str, Any], symbol: str) -> dict[str, Any]:
    ex, o = result.get("execution") or {}, result.get("order") or {}
    if ex.get("status") == "submitted":
        what = f"${float(o['notional']):,.2f}" if o.get("notional") else f"{o.get('qty')} shares"
        at = f" at ${float(o['limit_price']):,.2f} or less" if o.get("limit_price") else ""
        till = ", until canceled" if o.get("good_until") == "gtc" else ""
        _notify(user.user_id, "orders", alerts.order_text("placed", symbol.upper(), f"Buy {what}{at}{till}."),
                [[{"text": "View in the app", "url": f"{_base_url()}/stock/{symbol.upper()}"}]])
    return result


@app.delete("/api/orders/{order_id}")
def cancel_order(order_id: str, aitrader_session: str | None = Cookie(default=None)) -> dict[str, Any]:
    user = _require_user(aitrader_session)
    broker = _broker_for_user(user.user_id)
    result = broker.cancel_order(order_id)
    if result.get("status") != "canceled":
        raise HTTPException(status_code=409, detail=result.get("message") or "Could not cancel.")
    try:
        o = broker.get_order(order_id)
        what = f"${float(o['notional']):,.2f}" if o.get("notional") else f"{o.get('qty')} shares"
        at = f" at ${float(o['limit_price']):,.2f}" if o.get("limit_price") else ""
        _notify(user.user_id, "orders", alerts.order_text("canceled", o.get("symbol") or "", f"Buy {what}{at}. Nothing was bought."))
    except Exception:  # noqa: BLE001
        pass
    return result


@app.get("/api/search")
def search_stocks(q: str = "", aitrader_session: str | None = Cookie(default=None)) -> dict[str, Any]:
    _require_user(aitrader_session)
    return {"results": market.search(q[:40])}


@app.get("/api/stock/{symbol}/overview")
def stock_overview(symbol: str, aitrader_session: str | None = Cookie(default=None)) -> dict[str, Any]:
    """Everything the stock page shows, in one call."""
    user = _require_user(aitrader_session)
    symbol = symbol.upper().strip()
    alpaca = _alpaca_data_for(user)
    try:
        stats = market.stats(symbol, alpaca)
    except market.MarketDataError as exc:
        raise HTTPException(status_code=404, detail=f"Could not find {symbol}.") from exc
    try:
        ctx = market.context(symbol)
    except market.MarketDataError:
        ctx = {}
    run = picks_store.latest()
    pick = next((p for p in (run or {}).get("picks", []) if p["symbol"] == symbol), None)
    broker = _broker_for_user(user.user_id)
    position, orders = None, []
    if broker.configured:
        try:
            position = next((p for p in broker.positions() if p.get("symbol") == symbol), None)
            orders = [o for o in broker.open_orders() if o.get("symbol") == symbol]
        except BrokerError:
            pass
    restricted = None
    if (stats.get("price") or 0) < 1:
        restricted = "Penny stocks (under $1) aren't available here."
    elif market.is_leveraged_or_inverse(stats.get("name")):
        restricted = "Leveraged and inverse funds aren't available here."
    status = broker.status() if broker.configured else {}
    return {
        "stats": stats, "context": ctx, "pick": pick,
        "run_id": run["run_id"] if run and pick else None,
        "bought_from_picks": bool(run and pick and symbol in _bought_from(run["run_id"], user.user_id)),
        "position": position, "open_orders": orders,
        "restricted": restricted,
        "can_trade": broker.configured and not user.is_demo,
        "is_demo": user.is_demo,
        "broker": broker.name, "mode": "live" if getattr(broker, "live", False) else "paper",
        "buying_power": status.get("buying_power"),
        "default_amount": f"{settings.default_budget_usd:.2f}",
    }


@app.get("/api/stock/{symbol}/history")
def stock_history(symbol: str, range: str = "1M", aitrader_session: str | None = Cookie(default=None)) -> dict[str, Any]:
    user = _require_user(aitrader_session)
    try:
        return market.history(symbol, range, _alpaca_data_for(user))
    except market.MarketDataError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/api/stock/{symbol}/context")
def stock_context(symbol: str, aitrader_session: str | None = Cookie(default=None)) -> dict[str, Any]:
    _require_user(aitrader_session)
    try:
        return market.context(symbol)
    except market.MarketDataError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/api/stock/{symbol}/intraday")
def stock_intraday(symbol: str, aitrader_session: str | None = Cookie(default=None)) -> dict[str, Any]:
    _require_user(aitrader_session)
    try:
        return market.intraday(symbol)
    except market.MarketDataError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/api/orders/{order_id}")
def order_status(order_id: str, aitrader_session: str | None = Cookie(default=None)) -> dict[str, Any]:
    user = _require_user(aitrader_session)
    try:
        order = _broker_for_user(user.user_id).get_order(order_id)
    except BrokerError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if not order:
        raise HTTPException(status_code=404, detail="Order not found.")
    return order


@app.get("/api/portfolio")
def portfolio(aitrader_session: str | None = Cookie(default=None)) -> dict[str, Any]:
    """Money at a glance: value, cash, invested, what is open, and how it is doing."""
    user = _require_user(aitrader_session)
    broker = _broker_for_user(user.user_id)
    if not broker.configured:
        return {"connected": False}
    try:
        acct = broker.account()
        positions = broker.positions()
        open_orders = broker.open_orders()
    except BrokerError as exc:
        return {"connected": True, "error": str(exc)}
    invested = sum(p.get("market_value") or 0 for p in positions)
    cost = sum((p.get("avg_entry_price") or 0) * (p.get("quantity") or 0) for p in positions)
    unrealized = sum(p.get("unrealized_pl") or 0 for p in positions)
    total = acct.get("total_value") or 0
    last = acct.get("last_equity") or 0
    return {
        "connected": True,
        "broker": broker.name,
        "mode": "live" if getattr(broker, "live", False) else "paper",
        "total_value": total,
        "cash": acct.get("cash_available") or 0,
        "buying_power": acct.get("buying_power") or 0,
        "invested": invested,
        "unrealized_pl": unrealized,
        "unrealized_pl_pct": (unrealized / cost * 100) if cost else None,
        "day_change": (total - last) if last else None,
        "day_change_pct": ((total / last - 1) * 100) if last else None,
        "positions": positions,
        "open_orders": open_orders,
    }


@app.get("/api/cron/research")
def cron_research(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    """Weekday-morning scheduled run so picks are ready before anyone logs in."""
    if not settings.cron_secret or authorization != f"Bearer {settings.cron_secret}":
        raise HTTPException(status_code=401, detail="Unauthorized.")
    run = picks.run_now()
    return {"run_id": run["run_id"], "status": run["status"], "picks": len(run["picks"]),
            "took_seconds": run.get("took_seconds"), "alerts_sent": _broadcast_picks(run)}
