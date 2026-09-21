"""Brokers behind one interface.

The decision service calls `place_notional_buy` and nothing else to trade, so the
approval gate does not care which broker is on the other side.

Alpaca is paper by default. Live trading needs ALPACA_LIVE=true set explicitly;
nothing in the app can flip it. Each order also re-checks the budget ceiling and
long-only rule here, independently of the decision service, and uses the decision
id as `client_order_id` so a duplicated call cannot create a second order.
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
        return {"broker": self.name, "configured": False,
                "message": "No broker connected. Add Alpaca paper keys to trade."}

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


class AlpacaBroker:
    name = "alpaca"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.live = settings.alpaca_live
        self.base = ALPACA_LIVE if self.live else ALPACA_PAPER

    @property
    def configured(self) -> bool:
        return bool(self.settings.alpaca_key_id and self.settings.alpaca_secret_key)

    def _headers(self) -> dict[str, str]:
        return {
            "APCA-API-KEY-ID": str(self.settings.alpaca_key_id),
            "APCA-API-SECRET-KEY": str(self.settings.alpaca_secret_key),
        }

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
        return {"period": period, "timeframe": timeframe, "base_value": h.get("base_value"), "points": points}

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
        symbol = symbol.upper().strip()
        ceiling = self.settings.max_budget_usd
        if notional <= 0 or notional > ceiling:
            return {"status": "blocked", "message": f"Order ${notional} is outside the ${ceiling} ceiling."}
        if not self.configured:
            return {"status": "blocked", "message": "Alpaca keys are not set."}

        try:
            asset = self._get(f"/v2/assets/{symbol}")
        except BrokerError as exc:
            return {"status": "blocked", "message": f"Could not verify {symbol}: {exc}"}
        if not asset.get("tradable"):
            return {"status": "blocked", "message": f"{symbol} is not tradable on Alpaca."}
        if not asset.get("fractionable"):
            return {"status": "blocked", "message": f"{symbol} does not support dollar-amount orders."}
        if asset.get("class") != "us_equity":
            return {"status": "blocked", "message": f"{symbol} is not a US equity."}

        try:
            r = httpx.post(
                f"{self.base}/v2/orders",
                headers=self._headers(),
                json={
                    "symbol": symbol,
                    "notional": f"{notional:.2f}",
                    "side": "buy",
                    "type": "market",
                    "time_in_force": "day",
                    "client_order_id": client_order_id,
                },
                timeout=20,
            )
        except httpx.HTTPError as exc:
            return {"status": "failed", "message": f"Could not reach Alpaca: {type(exc).__name__}"}

        if r.status_code >= 400:
            detail = r.json().get("message") if "json" in r.headers.get("content-type", "") else r.text[:200]
            return {"status": "rejected", "message": f"Alpaca rejected the order: {detail}"}

        o = r.json()
        return {
            "status": "submitted",
            "broker": self.name,
            "mode": "live" if self.live else "paper",
            "order_id": o.get("id"),
            "client_order_id": o.get("client_order_id"),
            "symbol": o.get("symbol"),
            "notional": o.get("notional"),
            "order_status": o.get("status"),
            "message": f"{'Live' if self.live else 'Paper'} order submitted to Alpaca.",
        }


def build_broker(settings: Settings) -> Broker:
    if settings.alpaca_key_id and settings.alpaca_secret_key:
        return AlpacaBroker(settings)
    return NullBroker()
