"""Per-user Telegram alerts.

One bot serves every user. A user links their own Telegram chat by opening a
one-time link (t.me/<bot>?start=<code>); the code is signed and short-lived, so
only the signed-in user who asked for it can bind a chat to their account. From
then on that chat, and only that chat, receives the user's alerts and may press
their buttons.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from html import escape
from pathlib import Path
from typing import Any

LINK_TTL_SECONDS = 15 * 60
DEFAULT_PREFS = {"picks": True, "orders": True}


# -- one-time link codes -------------------------------------------------------

def _b36(n: int) -> str:
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = ""
    while n:
        n, r = divmod(n, 36)
        out = digits[r] + out
    return out or "0"


def _sig(secret: str, body: str) -> str:
    return hmac.new(secret.encode(), f"tg-link:{body}".encode(), hashlib.sha256).hexdigest()[:20]


def make_link_code(secret: str, user_id: str, *, now: float | None = None) -> str:
    """`<user>_<expiry>_<sig>`: fits Telegram's 64-char start parameter ([A-Za-z0-9_-])."""
    uid = user_id.removeprefix("usr_")
    body = f"{uid}_{_b36(int((now or time.time()) + LINK_TTL_SECONDS))}"
    return f"{body}_{_sig(secret, body)}"


def read_link_code(secret: str, code: str, *, now: float | None = None) -> str | None:
    """The user id the code was issued to, or None if it is forged or expired."""
    parts = (code or "").split("_")
    if len(parts) != 3:
        return None
    uid, exp, sig = parts
    body = f"{uid}_{exp}"
    if not hmac.compare_digest(sig, _sig(secret, body)):
        return None
    try:
        if int(exp, 36) < (now or time.time()):
            return None
    except ValueError:
        return None
    return f"usr_{uid}"


def webhook_secret(settings: Any) -> str:
    return settings.telegram_webhook_secret or hmac.new(
        settings.session_secret.encode(), b"telegram-webhook", hashlib.sha256).hexdigest()[:48]


# -- link storage --------------------------------------------------------------

def _row(user_id: str, chat_id: str, username: str | None, name: str | None,
         prefs: dict[str, Any] | None = None, linked_at: float | None = None) -> dict[str, Any]:
    return {"user_id": user_id, "chat_id": str(chat_id), "username": username, "name": name,
            "linked_at": linked_at or time.time(), "prefs": {**DEFAULT_PREFS, **(prefs or {})}}


class FileLinkStore:
    """`chat_id` is the Telegram chat id, or the phone number for iMessage."""

    def __init__(self, root: str | Path, filename: str = "telegram_links.json") -> None:
        self.path = Path(root) / filename

    def _read(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _write(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def link(self, user_id: str, chat_id: str, username: str | None, name: str | None) -> dict[str, Any]:
        data = self._read()
        for uid in [u for u, r in data.items() if r["chat_id"] == str(chat_id)]:
            data.pop(uid)  # a chat belongs to one account at a time
        prev = data.get(user_id) or {}
        data[user_id] = _row(user_id, chat_id, username, name, prev.get("prefs"))
        self._write(data)
        return data[user_id]

    def get(self, user_id: str) -> dict[str, Any] | None:
        return self._read().get(user_id)

    def by_chat(self, chat_id: str) -> dict[str, Any] | None:
        return next((r for r in self._read().values() if r["chat_id"] == str(chat_id)), None)

    def all(self) -> list[dict[str, Any]]:
        return list(self._read().values())

    def set_prefs(self, user_id: str, prefs: dict[str, Any]) -> dict[str, Any] | None:
        data = self._read()
        if user_id not in data:
            return None
        data[user_id]["prefs"] = {**DEFAULT_PREFS, **data[user_id].get("prefs", {}), **prefs}
        self._write(data)
        return data[user_id]

    def unlink(self, user_id: str) -> bool:
        data = self._read()
        gone = data.pop(user_id, None) is not None
        self._write(data)
        return gone


class PostgresLinkStore:
    SCHEMA = """
    CREATE TABLE IF NOT EXISTS {table} (
        user_id   TEXT PRIMARY KEY,
        chat_id   TEXT UNIQUE NOT NULL,
        username  TEXT,
        name      TEXT,
        linked_at DOUBLE PRECISION NOT NULL,
        prefs     JSONB NOT NULL
    );
    """

    def __init__(self, db: Any, table: str = "telegram_links") -> None:
        assert table.isidentifier()
        self.db = db
        self.t = table
        self._ready = False

    def _conn(self):
        conn = self.db.connect()
        if not self._ready:
            with conn.cursor() as cur:
                cur.execute(self.SCHEMA.format(table=self.t))
            conn.commit()
            self._ready = True
        return conn

    @staticmethod
    def _out(r: dict[str, Any] | None) -> dict[str, Any] | None:
        if not r:
            return None
        prefs = r["prefs"] if isinstance(r["prefs"], dict) else json.loads(r["prefs"])
        return {**r, "prefs": {**DEFAULT_PREFS, **prefs}}

    def link(self, user_id: str, chat_id: str, username: str | None, name: str | None) -> dict[str, Any]:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(f"DELETE FROM {self.t} WHERE chat_id = %s AND user_id <> %s", (str(chat_id), user_id))
            cur.execute(
                f"""INSERT INTO {self.t} (user_id, chat_id, username, name, linked_at, prefs)
                   VALUES (%s, %s, %s, %s, %s, %s)
                   ON CONFLICT (user_id) DO UPDATE SET chat_id = EXCLUDED.chat_id, username = EXCLUDED.username,
                       name = EXCLUDED.name, linked_at = EXCLUDED.linked_at
                   RETURNING *""",
                (user_id, str(chat_id), username, name, time.time(), json.dumps(DEFAULT_PREFS)))
            return self._out(cur.fetchone())

    def get(self, user_id: str) -> dict[str, Any] | None:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(f"SELECT * FROM {self.t} WHERE user_id = %s", (user_id,))
            return self._out(cur.fetchone())

    def by_chat(self, chat_id: str) -> dict[str, Any] | None:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(f"SELECT * FROM {self.t} WHERE chat_id = %s", (str(chat_id),))
            return self._out(cur.fetchone())

    def all(self) -> list[dict[str, Any]]:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(f"SELECT * FROM {self.t}")
            return [self._out(r) for r in cur.fetchall()]

    def set_prefs(self, user_id: str, prefs: dict[str, Any]) -> dict[str, Any] | None:
        row = self.get(user_id)
        if not row:
            return None
        merged = {**row["prefs"], **prefs}
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(f"UPDATE {self.t} SET prefs = %s WHERE user_id = %s RETURNING *",
                        (json.dumps(merged), user_id))
            return self._out(cur.fetchone())

    def unlink(self, user_id: str) -> bool:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(f"DELETE FROM {self.t} WHERE user_id = %s", (user_id,))
            return cur.rowcount > 0


# -- messages ------------------------------------------------------------------

def _money(v: Any) -> str:
    try:
        f = float(v)
        return f"${f:,.0f}" if f == int(f) and f >= 10 else f"${f:,.2f}"
    except (TypeError, ValueError):
        return str(v)


def picks_digest(run: dict[str, Any], base_url: str, amount: Any) -> tuple[str, list[list[dict[str, str]]]]:
    picks = run.get("picks") or []
    buys = [p for p in picks if p.get("verdict") == "buy"]
    watch = [p["symbol"] for p in picks if p.get("verdict") == "watch"]
    avoid = [p["symbol"] for p in picks if p.get("verdict") == "avoid"]
    day = time.strftime("%a %b %-d", time.localtime(run.get("created_at") or time.time()))
    lines = [f"<b>Today's picks</b> · {day}", ""]
    if buys:
        for p in buys:
            lines.append(f"🟢 <b>{escape(p['symbol'])}</b> · Buy")
            lines.append(escape(str(p.get("summary") or ""))[:420])
            lines.append("")
    else:
        lines += ["No buys today. Nothing cleared the bar, so sitting out is the call.", ""]
    if watch:
        lines.append(f"Watch: {escape(', '.join(watch))}")
    if avoid:
        lines.append(f"Avoid: {escape(', '.join(avoid))}")
    lines += ["", "<i>Not investment advice.</i>"]
    rows = [[{"text": f"Buy {_money(amount)} of {p['symbol']}", "callback_data": f"pb:{p['symbol']}"},
             {"text": "Details", "url": f"{base_url}/stock/{p['symbol']}"}] for p in buys[:5]]
    rows.append([{"text": "Open today's picks", "url": f"{base_url}/app"}])
    return "\n".join(lines).strip(), rows


def confirm_text(symbol: str, amount: Any, price: Any, mode: str, market_open: bool) -> str:
    acct = "real money" if mode == "live" else "your paper account (practice money)"
    when = "" if market_open else "\nThe market is closed, so it will fill after 9:30 AM ET."
    return (f"Buy <b>{_money(amount)}</b> of <b>{escape(symbol)}</b> at about {_money(price)}?\n"
            f"It goes to {acct}.{when}")


def order_text(kind: str, symbol: str, detail: str) -> str:
    head = {"placed": "Order sent", "filled": "Order filled", "canceled": "Order canceled",
            "failed": "Order not placed"}.get(kind, kind)
    return f"<b>{head}</b> · {escape(symbol)}\n{escape(detail)}"


def imessage_digest(run: dict[str, Any], base_url: str, amount: Any) -> tuple[str, dict[str, Any] | None, list[str]]:
    """Plain text (iMessage has no formatting), an optional Buy/Skip poll, and the symbols it offers."""
    picks = run.get("picks") or []
    buys = [p for p in picks if p.get("verdict") == "buy"][:3]
    watch = [p["symbol"] for p in picks if p.get("verdict") == "watch"]
    day = time.strftime("%a %b %-d", time.localtime(run.get("created_at") or time.time()))
    lines = [f"AI Trader · Today's picks, {day}", ""]
    for p in buys:
        summary = str(p.get("summary") or "")
        lines += [f"{p['symbol']} · Buy", summary[:260] + ("…" if len(summary) > 260 else ""), ""]
    if not buys:
        lines += ["No buys today. Nothing cleared the bar, so sitting out is the call.", ""]
    if watch:
        lines += [f"Watch: {', '.join(watch[:6])}", ""]
    syms = [p["symbol"] for p in buys]
    if syms:
        how = (f"Reply YES to buy {_money(amount)} of {syms[0]}, or NO to skip."
               if len(syms) == 1 else f"Reply {' or '.join(syms)} to buy {_money(amount)}, or NO to skip.")
        lines += [how, ""]
    lines += [f"Details: {base_url}/app", "Not investment advice."]
    poll = ({"title": f"Buy {_money(amount)} today?", "options": [f"Buy {s}" for s in syms] + ["Skip today"]}
            if syms else None)
    return "\n".join(lines), poll, syms
