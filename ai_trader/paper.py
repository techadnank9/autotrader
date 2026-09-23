"""A practice account the platform runs itself.

A user who does not want to connect a brokerage starts with $100,000 of practice
money. We keep their cash, positions and orders ourselves and fill at the same
live prices the rest of the app shows, so nothing is shared between users the way
one pooled brokerage account would be.

Orders settle lazily: every read settles first, and so does the daily research
cron. A limit order therefore fills the next time we look, not tick by tick.
"""

from __future__ import annotations

import json
import time
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

START_CASH = 100_000.0
OPEN_STATES = {"new", "accepted", "held"}


def _now() -> float:
    return time.time()


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _blank() -> dict[str, Any]:
    return {"cash": START_CASH, "positions": {}, "orders": [], "equity": [],
            "opened_at": _now(), "start_cash": START_CASH}


class FilePaperStore:
    def __init__(self, root: str | Path) -> None:
        self.path = Path(root) / "paper_accounts.json"

    def _read(self) -> dict[str, Any]:
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def get(self, user_id: str) -> dict[str, Any] | None:
        return self._read().get(user_id)

    def put(self, user_id: str, data: dict[str, Any]) -> None:
        d = self._read()
        d[user_id] = data
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(d, indent=2), encoding="utf-8")

    def all_ids(self) -> list[str]:
        return list(self._read().keys())

    def delete(self, user_id: str) -> bool:
        d = self._read()
        gone = d.pop(user_id, None) is not None
        self.path.write_text(json.dumps(d, indent=2), encoding="utf-8")
        return gone


class PostgresPaperStore:
    SCHEMA = """
    CREATE TABLE IF NOT EXISTS paper_accounts (
        user_id TEXT PRIMARY KEY,
        data    JSONB NOT NULL
    );
    """

    def __init__(self, db: Any) -> None:
        self.db = db
        self._ready = False

    def _conn(self):
        conn = self.db.connect()
        if not self._ready:
            with conn.cursor() as cur:
                cur.execute(self.SCHEMA)
            conn.commit()
            self._ready = True
        return conn

    def get(self, user_id: str) -> dict[str, Any] | None:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT data FROM paper_accounts WHERE user_id = %s", (user_id,))
            row = cur.fetchone()
            if not row:
                return None
            d = row["data"]
            return d if isinstance(d, dict) else json.loads(d)

    def put(self, user_id: str, data: dict[str, Any]) -> None:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute("""INSERT INTO paper_accounts (user_id, data) VALUES (%s, %s)
                           ON CONFLICT (user_id) DO UPDATE SET data = EXCLUDED.data""",
                        (user_id, json.dumps(data)))

    def all_ids(self) -> list[str]:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT user_id FROM paper_accounts")
            return [r["user_id"] for r in cur.fetchall()]

    def delete(self, user_id: str) -> bool:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM paper_accounts WHERE user_id = %s", (user_id,))
            return cur.rowcount > 0


class PaperBroker:
    """Same surface as AlpacaBroker, backed by our own ledger."""

    name = "paper"
    live = False

    def __init__(self, store: Any, user_id: str, quotes: Callable[[list[str]], dict[str, Any]],
                 max_order_usd: Decimal) -> None:
        self.store = store
        self.user_id = user_id
        self._quotes = quotes
        self.max_order_usd = max_order_usd
        self._acct: dict[str, Any] | None = None

    # -- ledger ----------------------------------------------------------------

    @property
    def configured(self) -> bool:
        return self.store.get(self.user_id) is not None

    def _load(self) -> dict[str, Any]:
        if self._acct is None:
            self._acct = self.store.get(self.user_id) or _blank()
        return self._acct

    def _save(self) -> None:
        if self._acct is not None:
            self.store.put(self.user_id, self._acct)

    def _prices(self, symbols: list[str]) -> dict[str, float]:
        if not symbols:
            return {}
        try:
            return self._quotes(sorted(set(symbols)))
        except Exception:  # noqa: BLE001  a price outage must not wipe the ledger
            return {}

    # -- settlement ------------------------------------------------------------

    def _settle(self) -> dict[str, Any]:
        """Fill what the market would have filled since we last looked."""
        a = self._load()
        open_orders = [o for o in a["orders"] if o["status"] in OPEN_STATES]
        symbols = [o["symbol"] for o in open_orders] + list(a["positions"].keys())
        quotes = self._prices(symbols)
        market_open = bool(quotes.get("__open__"))
        changed = False

        for o in open_orders:
            px = quotes.get(o["symbol"])
            if px is None:
                continue
            if o["side"] == "buy":
                if not market_open:
                    continue  # queued until the market opens
                limit = o.get("limit_price")
                if limit is not None and px > float(limit):
                    continue
                fill = min(px, float(limit)) if limit is not None else px
                qty = float(o["qty"]) if o.get("qty") else round(float(o["notional"]) / fill, 6)
                cost = qty * fill
                if cost > a["cash"] + 1e-6:
                    o.update(status="rejected", canceled_at=_iso(_now()),
                             reject_reason="Not enough practice cash.")
                    changed = True
                    continue
                a["cash"] -= cost
                pos = a["positions"].setdefault(o["symbol"], {"qty": 0.0, "cost": 0.0})
                pos["qty"] += qty
                pos["cost"] += cost
                o.update(status="filled", filled_qty=f"{qty:.6f}", filled_avg_price=f"{fill:.2f}",
                         filled_at=_iso(_now()))
                changed = True
                if o.get("take_profit") or o.get("stop_loss"):
                    a["orders"].append({
                        "id": uuid.uuid4().hex, "symbol": o["symbol"], "side": "sell",
                        "status": "held", "qty": f"{qty:.6f}", "notional": None,
                        "limit_price": o.get("take_profit"), "stop_price": o.get("stop_loss"),
                        "time_in_force": "gtc", "submitted_at": _iso(_now()), "accepted_at": _iso(_now()),
                        "parent": o["id"],
                    })
            else:  # a sell: plain, at a price, or an exit leg watching a position
                tp = o.get("limit_price")
                sl = o.get("stop_price")
                hit = (True if tp is None and sl is None                      # sell now
                       else (tp is not None and px >= float(tp))             # at or above the price
                       or (sl is not None and px <= float(sl)))              # stop loss
                if not (market_open and hit):
                    continue
                pos = a["positions"].get(o["symbol"])
                if not pos or pos["qty"] <= 0:
                    o.update(status="canceled", canceled_at=_iso(_now()))
                    changed = True
                    continue
                qty = min(float(o["qty"]), pos["qty"])
                a["cash"] += qty * px
                pos["cost"] *= max(0.0, 1 - qty / pos["qty"]) if pos["qty"] else 0
                pos["qty"] -= qty
                if pos["qty"] <= 1e-9:
                    a["positions"].pop(o["symbol"], None)
                o.update(status="filled", filled_qty=f"{qty:.6f}", filled_avg_price=f"{px:.2f}",
                         filled_at=_iso(_now()))
                changed = True

        equity = a["cash"] + sum(p["qty"] * quotes.get(s, p["cost"] / p["qty"] if p["qty"] else 0)
                                 for s, p in a["positions"].items())
        today = time.strftime("%Y-%m-%d", time.gmtime())
        hist = a.setdefault("equity", [])
        if not hist or hist[-1].get("d") != today:
            hist.append({"d": today, "t": int(_now()), "equity": round(equity, 2)})
            changed = True
        else:
            hist[-1]["equity"] = round(equity, 2)
            changed = True
        if len(hist) > 400:
            del hist[:-400]
        if changed:
            self._save()
        a["_quotes"] = quotes
        return a

    # -- read surface ----------------------------------------------------------

    def status(self) -> dict[str, Any]:
        a = self._settle()
        return {"broker": self.name, "mode": "paper", "configured": True, "connected": True,
                "account_status": "ACTIVE", "trading_blocked": False,
                "buying_power": f"{a['cash']:.2f}",
                "market_open": bool((a.get("_quotes") or {}).get("__open__")),
                "message": "Practice account."}

    def verify(self) -> dict[str, Any]:
        return {"status": "ACTIVE"}

    def account(self) -> dict[str, Any]:
        a = self._settle()
        q = a.get("_quotes") or {}
        holdings = sum(p["qty"] * q.get(s, p["cost"] / p["qty"] if p["qty"] else 0) for s, p in a["positions"].items())
        equity = a["cash"] + holdings
        hist = a.get("equity") or []
        last = hist[-2]["equity"] if len(hist) >= 2 else a.get("start_cash", START_CASH)
        return {"total_value": round(equity, 2), "cash_available": round(a["cash"], 2),
                "buying_power": round(a["cash"], 2), "last_equity": last,
                "account_number_masked": "Practice", "status": "ACTIVE"}

    def positions(self) -> list[dict[str, Any]]:
        a = self._settle()
        q = a.get("_quotes") or {}
        out = []
        for s, p in a["positions"].items():
            avg = p["cost"] / p["qty"] if p["qty"] else 0
            px = q.get(s, avg)
            value = p["qty"] * px
            out.append({"symbol": s, "quantity": round(p["qty"], 6), "market_value": round(value, 2),
                        "current_price": round(px, 2), "avg_entry_price": round(avg, 2),
                        "unrealized_pl": round(value - p["cost"], 2),
                        "unrealized_plpc": round((value - p["cost"]) / p["cost"], 4) if p["cost"] else 0.0})
        return sorted(out, key=lambda x: -x["market_value"])

    def portfolio_history(self, period: str = "1M") -> dict[str, Any]:
        a = self._settle()
        days = {"1D": 2, "1W": 8, "1M": 31, "3M": 93, "1A": 366, "1Y": 366}.get(period, 31)
        hist = (a.get("equity") or [])[-days:]
        start = a.get("start_cash", START_CASH)
        points = [{"t": h["t"], "equity": h["equity"], "pl": round(h["equity"] - start, 2)} for h in hist]
        return {"period": period, "timeframe": "1D", "base_value": start, "points": points,
                "opened_in_period": len(a.get("equity") or []) <= days}

    def _view(self, o: dict[str, Any]) -> dict[str, Any]:
        return {"id": o["id"], "symbol": o["symbol"], "side": o["side"], "status": o["status"],
                "order_class": "bracket" if o.get("take_profit") or o.get("stop_loss") else "simple",
                "notional": o.get("notional"), "qty": o.get("qty"),
                "filled_qty": o.get("filled_qty"), "filled_avg_price": o.get("filled_avg_price"),
                "submitted_at": o.get("submitted_at"), "accepted_at": o.get("accepted_at"),
                "filled_at": o.get("filled_at"), "canceled_at": o.get("canceled_at"),
                "type": "limit" if o.get("limit_price") else "market",
                "limit_price": o.get("limit_price"), "time_in_force": o.get("time_in_force"),
                "cancelable": o["status"] in OPEN_STATES,
                "take_profit": o.get("take_profit"), "stop_loss": o.get("stop_loss"),
                "message": o.get("reject_reason")}

    def recent_orders(self, limit: int = 50) -> list[dict[str, Any]]:
        a = self._settle()
        return [self._view(o) for o in sorted(a["orders"], key=lambda x: x["submitted_at"], reverse=True)[:limit]]

    def open_orders(self) -> list[dict[str, Any]]:
        a = self._settle()
        return [self._view(o) for o in a["orders"] if o["status"] in OPEN_STATES]

    def get_order(self, order_id: str) -> dict[str, Any]:
        a = self._settle()
        o = next((x for x in a["orders"] if x["id"] == order_id), None)
        if not o:
            from ai_trader.brokers import BrokerError
            raise BrokerError("That order was not found.")
        return self._view(o)

    def cancel_order(self, order_id: str) -> dict[str, Any]:
        a = self._load()
        o = next((x for x in a["orders"] if x["id"] == order_id), None)
        if not o:
            return {"status": "failed", "message": "That order was not found."}
        if o["status"] not in OPEN_STATES:
            return {"status": "failed", "message": "That order can no longer be canceled."}
        o.update(status="canceled", canceled_at=_iso(_now()))
        self._save()
        return {"status": "canceled", "message": "Order canceled."}

    # -- write surface ---------------------------------------------------------

    def place_notional_buy(self, symbol: str, notional: Decimal, client_order_id: str) -> dict[str, Any]:
        return self.place_buy(symbol, notional=notional, client_order_id=client_order_id)

    def place_buy(self, symbol: str, *, client_order_id: str, notional: Decimal | None = None,
                  qty: Decimal | None = None, take_profit: Decimal | None = None,
                  stop_loss: Decimal | None = None, est_price: Decimal | None = None,
                  limit_price: Decimal | None = None, good_until: str = "day") -> dict[str, Any]:
        a = self._settle()
        cost = float(notional) if notional is not None else float(qty or 0) * float(limit_price or est_price or 0)
        if cost > a["cash"] + 1e-6:
            return {"status": "failed",
                    "message": f"That is more than your practice cash (${a['cash']:,.2f})."}
        order = {
            "id": uuid.uuid4().hex, "client_order_id": client_order_id, "symbol": symbol.upper(),
            "side": "buy", "status": "accepted",
            "notional": f"{float(notional):.2f}" if notional is not None else None,
            "qty": f"{float(qty):.6f}".rstrip("0").rstrip(".") if qty is not None else None,
            "limit_price": f"{float(limit_price):.2f}" if limit_price is not None else None,
            "take_profit": f"{float(take_profit):.2f}" if take_profit is not None else None,
            "stop_loss": f"{float(stop_loss):.2f}" if stop_loss is not None else None,
            "time_in_force": good_until, "submitted_at": _iso(_now()), "accepted_at": _iso(_now()),
        }
        a["orders"].append(order)
        self._save()
        self._acct = None  # settle fresh so a market order fills right away
        self._settle()
        return {"status": "submitted", "order_id": order["id"], "mode": "paper"}

    def shares_available(self, symbol: str) -> float:
        """Shares held, minus any already committed to an open sell."""
        a = self._settle()
        held = (a["positions"].get(symbol.upper()) or {}).get("qty", 0.0)
        committed = sum(float(o.get("qty") or 0) for o in a["orders"]
                        if o["side"] == "sell" and o["status"] in OPEN_STATES and o["symbol"] == symbol.upper())
        return max(0.0, held - committed)

    def place_sell(self, symbol: str, *, client_order_id: str, qty: Decimal | None = None,
                   limit_price: Decimal | None = None, good_until: str = "day",
                   **_: Any) -> dict[str, Any]:
        symbol = symbol.upper().strip()
        a = self._settle()
        available = self.shares_available(symbol)
        if available <= 0:
            return {"status": "failed", "message": f"You don't hold any {symbol} to sell."}
        want = float(qty) if qty is not None else available
        if want > available + 1e-9:
            return {"status": "failed",
                    "message": f"You only have {available:.4f} {symbol} shares available to sell."}
        order = {
            "id": uuid.uuid4().hex, "client_order_id": client_order_id, "symbol": symbol,
            "side": "sell", "status": "accepted", "notional": None, "qty": f"{want:.6f}",
            "limit_price": f"{float(limit_price):.2f}" if limit_price is not None else None,
            "stop_price": None, "time_in_force": good_until,
            "submitted_at": _iso(_now()), "accepted_at": _iso(_now()),
        }
        a["orders"].append(order)
        self._save()
        self._acct = None
        self._settle()
        return {"status": "submitted", "order_id": order["id"], "mode": "paper"}

    def open(self) -> dict[str, Any]:
        """Create the account. Safe to call twice."""
        if self.store.get(self.user_id) is None:
            self.store.put(self.user_id, _blank())
            self._acct = None
        return self.account()
