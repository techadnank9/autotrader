"""Brokers behind one interface.

Every user connects their own brokerage, so a broker is built per request from
that user's decrypted credentials and never shared between users.

The decision service calls `place_notional_buy` and nothing else to trade, so the
approval gate does not care which broker is on the other side. Each order also
re-checks the ceiling and long-only rule here, independently, and uses the
decision id as `client_order_id` so a duplicated call cannot place a second order.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Protocol

import httpx

from ai_trader.config import Settings

ALPACA_PAPER = "https://paper-api.alpaca.markets"
ALPACA_LIVE = "https://api.alpaca.markets"


class BrokerError(RuntimeError):
    pass


class Broker(Protocol):
    name: str

    @property
    def configured(self) -> bool: ...
    def status(self) -> dict[str, Any]: ...
    def account(self) -> dict[str, Any]: ...
    def positions(self) -> list[dict[str, Any]]: ...
    def portfolio_history(self, period: str = "1M") -> dict[str, Any]: ...
    def recent_orders(self, limit: int = 50) -> list[dict[str, Any]]: ...
    def place_notional_buy(self, symbol: str, notional: Decimal, client_order_id: str) -> dict[str, Any]: ...


class NullBroker:
    name = "none"

    @property
    def configured(self) -> bool:
        return False

    def status(self) -> dict[str, Any]:
        return {"broker": self.name, "configured": False, "connected": False,
                "message": "No broker connected. Connect your Alpaca account in Profile."}

    def account(self) -> dict[str, Any]:
        return {}

    def positions(self) -> list[dict[str, Any]]:
        return []

    def portfolio_history(self, period: str = "1M") -> dict[str, Any]:
        return {"points": []}

    def recent_orders(self, limit: int = 50) -> list[dict[str, Any]]:
        return []

    def place_notional_buy(self, symbol: str, notional: Decimal, client_order_id: str) -> dict[str, Any]:
        return {"status": "blocked", "message": "No broker connected, so no order was placed."}

    def place_buy(self, symbol: str, **_: Any) -> dict[str, Any]:
        return {"status": "blocked", "message": "No broker connected, so no order was placed."}

    def get_order(self, order_id: str) -> dict[str, Any]:
        return {}

    def open_orders(self) -> list[dict[str, Any]]:
        return []


class AlpacaBroker:
    name = "alpaca"

    def __init__(self, key_id: str, secret_key: str, *, live: bool, max_order_usd: Decimal) -> None:
        self._key_id = key_id
        self._secret_key = secret_key
        self.live = live
        self.max_order_usd = max_order_usd
        self.base = ALPACA_LIVE if live else ALPACA_PAPER

    @property
    def configured(self) -> bool:
        return bool(self._key_id and self._secret_key)

    def _headers(self) -> dict[str, str]:
        return {"APCA-API-KEY-ID": self._key_id, "APCA-API-SECRET-KEY": self._secret_key}

    def verify(self) -> dict[str, Any]:
        """Prove the keys work before they are saved. Raises BrokerError if not."""
        acct = self._get("/v2/account")
        if acct.get("trading_blocked") or acct.get("account_blocked"):
            raise BrokerError("Alpaca reports this account is blocked from trading.")
        return {"status": acct.get("status"), "buying_power": acct.get("buying_power"),
                "currency": acct.get("currency")}

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        try:
            r = httpx.get(f"{self.base}{path}", headers=self._headers(), params=params, timeout=20)
        except httpx.HTTPError as exc:
            raise BrokerError(f"Could not reach Alpaca: {type(exc).__name__}") from exc
        if r.status_code == 401 or r.status_code == 403:
            raise BrokerError("Alpaca rejected the API keys.")
        if r.status_code >= 400:
            raise BrokerError(f"Alpaca error {r.status_code}: {r.text[:200]}")
        return r.json()

    def status(self) -> dict[str, Any]:
        base = {"broker": self.name, "mode": "live" if self.live else "paper", "configured": self.configured}
        if not self.configured:
            return {**base, "connected": False, "message": "Alpaca keys are not set."}
        try:
            acct = self._get("/v2/account")
            clock = self._get("/v2/clock")
        except BrokerError as exc:
            return {**base, "connected": False, "message": str(exc)}
        return {
            **base,
            "connected": True,
            "account_status": acct.get("status"),
            "trading_blocked": bool(acct.get("trading_blocked")),
            "buying_power": acct.get("buying_power"),
            "market_open": bool(clock.get("is_open")),
            "next_open": clock.get("next_open"),
            "message": "Connected.",
        }

    def account(self) -> dict[str, Any]:
        a = self._get("/v2/account")
        return {
            "total_value": float(a.get("equity") or 0),
            "cash_available": float(a.get("cash") or 0),
            "buying_power": float(a.get("buying_power") or 0),
            "last_equity": float(a.get("last_equity") or 0),
            "account_number_masked": ("•••• " + str(a.get("account_number", ""))[-4:]) if a.get("account_number") else None,
            "status": a.get("status"),
        }

    def positions(self) -> list[dict[str, Any]]:
        return [
            {
                "symbol": p.get("symbol"),
                "quantity": float(p.get("qty") or 0),
                "market_value": float(p.get("market_value") or 0),
                "current_price": float(p.get("current_price") or 0),
                "avg_entry_price": float(p.get("avg_entry_price") or 0),
                "unrealized_pl": float(p.get("unrealized_pl") or 0),
                "unrealized_plpc": float(p.get("unrealized_plpc") or 0),
            }
            for p in self._get("/v2/positions")
        ]

    def portfolio_history(self, period: str = "1M") -> dict[str, Any]:
        timeframe = {"1D": "15Min", "1W": "1H"}.get(period, "1D")
        h = self._get("/v2/account/portfolio/history", {"period": period, "timeframe": timeframe})
        points = [
            {"t": t, "equity": e, "pl": pl}
            for t, e, pl in zip(h.get("timestamp") or [], h.get("equity") or [], h.get("profit_loss") or [])
            if e is not None
        ]
        # Alpaca reports $0 for every day before the account was funded. Those days
        # are not a balance of zero, they are days the account did not exist; kept,
        # they draw the opening deposit as a gain. Start at the first funded day.
        first_funded = next((i for i, p in enumerate(points) if p["equity"] > 0), len(points))
        points = points[first_funded:]
        return {"period": period, "timeframe": timeframe, "base_value": h.get("base_value"),
                "points": points, "opened_in_period": first_funded > 0}

    def recent_orders(self, limit: int = 50) -> list[dict[str, Any]]:
        orders = self._get("/v2/orders", {"status": "all", "limit": limit, "direction": "desc"})
        return [
            {
                "id": o.get("id"),
                "client_order_id": o.get("client_order_id"),
                "symbol": o.get("symbol"),
                "side": o.get("side"),
                "notional": o.get("notional"),
                "filled_qty": o.get("filled_qty"),
                "filled_avg_price": o.get("filled_avg_price"),
                "status": o.get("status"),
                "submitted_at": o.get("submitted_at"),
                "filled_at": o.get("filled_at"),
            }
            for o in orders
        ]

    def place_notional_buy(self, symbol: str, notional: Decimal, client_order_id: str) -> dict[str, Any]:
        return self.place_buy(symbol, notional=notional, client_order_id=client_order_id)

    def place_buy(
        self,
        symbol: str,
        *,
        client_order_id: str,
        notional: Decimal | None = None,
        qty: Decimal | None = None,
        take_profit: Decimal | None = None,
        stop_loss: Decimal | None = None,
        est_price: Decimal | None = None,
    ) -> dict[str, Any]:
        """A long buy by dollar amount or by shares, optionally with an exit plan.

        Alpaca rules this follows: an exit plan is a bracket order, which needs
        whole shares and uses gtc so its take-profit and stop-loss legs outlive
        the trading day; dollar and fractional orders must be day orders; after
        4pm ET orders queue for the next session.
        """
        symbol = symbol.upper().strip()
        if not self.configured:
            return {"status": "blocked", "message": "No broker connected."}
        if (notional is None) == (qty is None):
            return {"status": "blocked", "message": "Choose a dollar amount or a number of shares."}
        if notional is not None and notional <= 0 or qty is not None and qty <= 0:
            return {"status": "blocked", "message": "Enter an amount greater than zero."}
        bracket = take_profit is not None or stop_loss is not None
        whole = qty is not None and qty == qty.to_integral_value()
        if bracket and not whole:
            return {"status": "blocked", "message": "An exit plan needs a whole number of shares."}

        estimate = notional if notional is not None else (qty * est_price if est_price else None)
        if estimate is not None and estimate > self.max_order_usd:
            return {"status": "blocked", "message": f"Order is above the ${self.max_order_usd:,.2f} per-order limit."}

        try:
            asset = self._get(f"/v2/assets/{symbol}")
        except BrokerError as exc:
            return {"status": "blocked", "message": f"Could not verify {symbol}: {exc}"}
        if not asset.get("tradable") or asset.get("class") != "us_equity":
            return {"status": "blocked", "message": f"{symbol} cannot be traded here."}
        if (notional is not None or not whole) and not asset.get("fractionable"):
            return {"status": "blocked", "message": f"{symbol} can only be bought in whole shares."}

        body: dict[str, Any] = {"symbol": symbol, "side": "buy", "type": "market",
                                "client_order_id": client_order_id}
        if notional is not None:
            body.update(notional=f"{notional:.2f}", time_in_force="day")
        else:
            body.update(qty=str(int(qty)) if whole else f"{qty.normalize()}",
                        time_in_force="gtc" if bracket else "day")
        if bracket:
            body["order_class"] = "bracket"
            if take_profit is not None:
                body["take_profit"] = {"limit_price": f"{take_profit:.2f}"}
            if stop_loss is not None:
                body["stop_loss"] = {"stop_price": f"{stop_loss:.2f}"}

        try:
            r = httpx.post(f"{self.base}/v2/orders", headers=self._headers(), json=body, timeout=20)
        except httpx.HTTPError as exc:
            return {"status": "failed", "message": f"Could not reach Alpaca: {type(exc).__name__}"}
        if r.status_code >= 400:
            detail = r.json().get("message") if "json" in r.headers.get("content-type", "") else r.text[:200]
            return {"status": "rejected", "message": f"Alpaca rejected the order: {detail}"}
        o = r.json()
        return {"status": "submitted", "broker": self.name, "mode": "live" if self.live else "paper",
                "order_id": o.get("id"), "client_order_id": o.get("client_order_id"),
                "symbol": o.get("symbol"), "order_status": o.get("status"),
                "message": f"{'Live' if self.live else 'Paper'} order sent to Alpaca."}

    def _order_view(self, o: dict[str, Any]) -> dict[str, Any]:
        legs = o.get("legs") or []
        return {
            "id": o.get("id"), "symbol": o.get("symbol"), "side": o.get("side"),
            "status": o.get("status"), "order_class": o.get("order_class") or "simple",
            "notional": o.get("notional"), "qty": o.get("qty"),
            "filled_qty": o.get("filled_qty"), "filled_avg_price": o.get("filled_avg_price"),
            "submitted_at": o.get("submitted_at"), "accepted_at": o.get("created_at"),
            "filled_at": o.get("filled_at"), "canceled_at": o.get("canceled_at"),
            "take_profit": next((l.get("limit_price") for l in legs if l.get("type") == "limit"), None),
            "stop_loss": next((l.get("stop_price") for l in legs if l.get("type") in ("stop", "stop_limit")), None),
        }

    def get_order(self, order_id: str) -> dict[str, Any]:
        return self._order_view(self._get(f"/v2/orders/{order_id}", {"nested": "true"}))

    def open_orders(self) -> list[dict[str, Any]]:
        return [self._order_view(o) for o in self._get("/v2/orders", {"status": "open", "nested": "true", "limit": 100})]


def broker_for(creds: Any, settings: Settings) -> Broker:
    """The broker for one user, or NullBroker if they have not connected one."""
    if creds is None:
        return NullBroker()
    if creds.provider == "alpaca":
        if creds.live and not settings.allow_live_trading:
            return NullBroker()  # live keys saved earlier stay inert while live is disabled
        return AlpacaBroker(creds.key_id, creds.secret_key, live=creds.live,
                            max_order_usd=settings.max_budget_usd)
    return NullBroker()
