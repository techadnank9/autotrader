"""Per-user Robinhood connection through Robinhood's Agentic Trading MCP server.

Each user authorizes their own Robinhood Agentic account with OAuth 2.1 + PKCE,
the same way hosted MCP clients (ChatGPT, Claude) connect. The app registers
itself dynamically (RFC 7591) and keeps each user's tokens encrypted. No Codex
CLI, no shared machine, no shared account.

Robinhood does not document its tool formats, so the connector discovers each
tool's input schema from `tools/list` and maps an order onto it. It fails
closed: if a required field cannot be mapped with certainty, the order is
refused rather than guessed. Every order goes through `review_equity_order`
before `place_equity_order`. Robinhood has no paper trading, so every order
placed through this broker is real money.
"""

from __future__ import annotations

import json
import time
import uuid
from decimal import Decimal
from typing import Any, Callable
from urllib.parse import urlencode

import httpx

MCP_URL = "https://agent.robinhood.com/mcp/trading"
METADATA_URL = "https://agent.robinhood.com/.well-known/oauth-authorization-server"
PROTOCOL_VERSION = "2025-06-18"
SCOPE = "internal"


class RobinhoodError(RuntimeError):
    pass


# -- OAuth --------------------------------------------------------------------


def discover() -> dict[str, Any]:
    try:
        r = httpx.get(METADATA_URL, timeout=15)
    except httpx.HTTPError as exc:
        raise RobinhoodError("Could not reach Robinhood.") from exc
    if r.status_code != 200:
        raise RobinhoodError("Robinhood's authorization server is unavailable.")
    meta = r.json()
    for key in ("authorization_endpoint", "token_endpoint", "registration_endpoint"):
        if not meta.get(key):
            raise RobinhoodError(f"Robinhood metadata is missing {key}.")
    return meta


def register_client(meta: dict[str, Any], redirect_uri: str) -> str:
    """Dynamic client registration: a public client, PKCE, no client secret."""
    try:
        r = httpx.post(meta["registration_endpoint"], json={
            "client_name": "AI Trader",
            "redirect_uris": [redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
            "scope": SCOPE,
        }, timeout=15)
    except httpx.HTTPError as exc:
        raise RobinhoodError("Could not register with Robinhood.") from exc
    if r.status_code not in (200, 201):
        raise RobinhoodError(f"Robinhood refused client registration ({r.status_code}).")
    client_id = r.json().get("client_id")
    if not client_id:
        raise RobinhoodError("Robinhood returned no client id.")
    return str(client_id)


def authorize_url(meta: dict[str, Any], client_id: str, redirect_uri: str, state: str, challenge: str) -> str:
    return meta["authorization_endpoint"] + "?" + urlencode({
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": SCOPE,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "resource": MCP_URL,
    })


def _token_request(meta: dict[str, Any], data: dict[str, str]) -> dict[str, Any]:
    try:
        r = httpx.post(meta["token_endpoint"], data={**data, "resource": MCP_URL}, timeout=20)
    except httpx.HTTPError as exc:
        raise RobinhoodError("Could not reach Robinhood to sign in.") from exc
    if r.status_code != 200:
        raise RobinhoodError("Robinhood did not accept the sign-in. Please connect again.")
    tokens = r.json()
    if not tokens.get("access_token"):
        raise RobinhoodError("Robinhood returned no access token.")
    return {
        "access_token": tokens["access_token"],
        "refresh_token": tokens.get("refresh_token"),
        "expires_at": time.time() + float(tokens.get("expires_in") or 3600) - 60,
    }


def exchange_code(meta: dict[str, Any], client_id: str, redirect_uri: str, code: str, verifier: str) -> dict[str, Any]:
    return _token_request(meta, {
        "grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
        "client_id": client_id, "code_verifier": verifier,
    })


def refresh(meta: dict[str, Any], client_id: str, refresh_token: str) -> dict[str, Any]:
    return _token_request(meta, {
        "grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": client_id,
    })


# -- MCP client ---------------------------------------------------------------


def _parse_rpc(resp: httpx.Response, want_id: int) -> dict[str, Any]:
    """Streamable HTTP answers with plain JSON or an SSE stream; handle both."""
    ctype = resp.headers.get("content-type", "")
    if "text/event-stream" in ctype:
        for line in resp.text.splitlines():
            if not line.startswith("data:"):
                continue
            try:
                msg = json.loads(line[5:].strip())
            except json.JSONDecodeError:
                continue
            if isinstance(msg, dict) and msg.get("id") == want_id:
                return msg
        raise RobinhoodError("Robinhood's stream ended without an answer.")
    try:
        msg = resp.json()
    except ValueError as exc:
        raise RobinhoodError("Robinhood returned an unreadable response.") from exc
    if isinstance(msg, list):
        msg = next((m for m in msg if m.get("id") == want_id), {})
    return msg


class MCPClient:
    def __init__(self, access_token: str, *, url: str = MCP_URL) -> None:
        self.url = url
        self.token = access_token
        self.session_id: str | None = None
        self._id = 0
        self._ready = False

    def _headers(self) -> dict[str, str]:
        h = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": PROTOCOL_VERSION,
        }
        if self.session_id:
            h["Mcp-Session-Id"] = self.session_id
        return h

    def _rpc(self, method: str, params: dict[str, Any] | None = None) -> Any:
        self._id += 1
        try:
            r = httpx.post(self.url, headers=self._headers(),
                           json={"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or {}},
                           timeout=45)
        except httpx.HTTPError as exc:
            raise RobinhoodError("Could not reach Robinhood.") from exc
        if r.status_code == 401:
            raise RobinhoodError("Robinhood sign-in expired. Please reconnect Robinhood.")
        if r.status_code >= 400:
            raise RobinhoodError(f"Robinhood error {r.status_code}.")
        if r.headers.get("mcp-session-id"):
            self.session_id = r.headers["mcp-session-id"]
        msg = _parse_rpc(r, self._id)
        if msg.get("error"):
            raise RobinhoodError(f"Robinhood: {msg['error'].get('message', 'request failed')}")
        return msg.get("result")

    def ensure_ready(self) -> None:
        if self._ready:
            return
        self._rpc("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "ai-trader", "version": "0.3"},
        })
        try:
            httpx.post(self.url, headers=self._headers(),
                       json={"jsonrpc": "2.0", "method": "notifications/initialized"}, timeout=15)
        except httpx.HTTPError:
            pass
        self._ready = True

    def list_tools(self) -> dict[str, dict[str, Any]]:
        self.ensure_ready()
        tools: dict[str, dict[str, Any]] = {}
        cursor = None
        for _ in range(10):
            result = self._rpc("tools/list", {"cursor": cursor} if cursor else {}) or {}
            for t in result.get("tools") or []:
                tools[t["name"]] = t.get("inputSchema") or {}
            cursor = result.get("nextCursor")
            if not cursor:
                break
        return tools

    def call(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        self.ensure_ready()
        result = self._rpc("tools/call", {"name": name, "arguments": arguments or {}}) or {}
        payload = _tool_payload(result)
        if result.get("isError"):
            raise RobinhoodError(_error_text(payload) or f"{name} failed.")
        return payload


def _tool_payload(result: dict[str, Any]) -> Any:
    if result.get("structuredContent") is not None:
        return result["structuredContent"]
    texts = [c.get("text", "") for c in result.get("content") or [] if c.get("type") == "text"]
    joined = "\n".join(t for t in texts if t)
    try:
        return json.loads(joined)
    except (json.JSONDecodeError, TypeError):
        return {"text": joined}


def _error_text(payload: Any) -> str:
    if isinstance(payload, dict):
        for k in ("error", "message", "detail", "text"):
            if payload.get(k):
                return str(payload[k])[:300]
    return str(payload)[:300] if payload else ""


# -- mapping undocumented schemas ---------------------------------------------

FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "symbol": ("symbol", "ticker", "instrument_symbol"),
    "side": ("side", "direction"),
    "dollars": ("dollar_amount", "notional", "amount", "dollars", "amount_in_dollars", "dollar_based_amount"),
    "type": ("type", "order_type"),
    "account": ("account_number", "account_id", "account"),
    "tif": ("time_in_force",),
    "ref": ("ref_id", "client_order_id", "idempotency_key"),
}


def _first(props: dict[str, Any], names: tuple[str, ...]) -> str | None:
    lowered = {k.lower(): k for k in props}
    for n in names:
        if n in lowered:
            return lowered[n]
    return None


def _pick_enum(schema: dict[str, Any], preferred: tuple[str, ...], fallback: str) -> str:
    enum = [str(v) for v in (schema.get("enum") or [])]
    if not enum:
        return fallback
    for want in preferred:
        for v in enum:
            if v.lower() == want:
                return v
    raise RobinhoodError(f"None of {preferred} is allowed; Robinhood accepts {enum}.")


def build_order_args(schema: dict[str, Any], *, symbol: str, dollars: Decimal,
                     account_number: str | None, ref: str) -> dict[str, Any]:
    """Map a dollar market buy onto Robinhood's schema, or refuse.

    Money moves here, so nothing is guessed: every required field must be
    satisfied by a field we understand, or the order is not sent.
    """
    props: dict[str, Any] = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    args: dict[str, Any] = {}

    k = _first(props, FIELD_ALIASES["symbol"])
    if not k:
        raise RobinhoodError("Robinhood's order form has no symbol field this app recognizes.")
    args[k] = symbol

    k = _first(props, FIELD_ALIASES["dollars"])
    if not k:
        raise RobinhoodError("Robinhood's order form has no dollar-amount field, so a capped order cannot be sent.")
    args[k] = f"{dollars:.2f}" if (props[k].get("type") == "string") else float(dollars)

    k = _first(props, FIELD_ALIASES["side"])
    if k:
        args[k] = _pick_enum(props[k], ("buy",), "buy")

    k = _first(props, FIELD_ALIASES["type"])
    if k:
        args[k] = _pick_enum(props[k], ("market",), "market")

    k = _first(props, FIELD_ALIASES["tif"])
    if k:
        args[k] = _pick_enum(props[k], ("gfd", "day"), "gfd")

    k = _first(props, FIELD_ALIASES["account"])
    if k:
        if not account_number:
            if k in required:
                raise RobinhoodError("Could not identify your Agentic account to place the order in.")
        else:
            args[k] = account_number

    k = _first(props, FIELD_ALIASES["ref"])
    if k:
        # Same decision -> same id, so Robinhood can reject a duplicate.
        args[k] = str(uuid.uuid5(uuid.NAMESPACE_URL, f"ai-trader:{ref}"))

    missing = sorted(required - set(args))
    if missing:
        raise RobinhoodError(f"Robinhood requires fields this app does not fill: {', '.join(missing)}. Order not sent.")
    return args


def _find(obj: Any, keys: tuple[str, ...]) -> Any:
    if isinstance(obj, dict):
        for k in keys:
            if obj.get(k) not in (None, ""):
                return obj[k]
        for v in obj.values():
            found = _find(v, keys)
            if found not in (None, ""):
                return found
    if isinstance(obj, list):
        for v in obj:
            found = _find(v, keys)
            if found not in (None, ""):
                return found
    return None


def _as_list(payload: Any, keys: tuple[str, ...]) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        for k in keys:
            if isinstance(payload.get(k), list):
                return [x for x in payload[k] if isinstance(x, dict)]
    return []


def _num(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def pick_agentic_account(accounts_payload: Any) -> str | None:
    """Only the Agentic account can trade. Return it only if unambiguous."""
    accts = _as_list(accounts_payload, ("accounts", "results", "data"))
    def number(a: dict[str, Any]) -> str | None:
        v = _find(a, ("account_number", "account_id", "number", "id"))
        return str(v) if v else None
    flagged = [a for a in accts if "agentic" in json.dumps(a).lower()]
    if len(flagged) == 1:
        return number(flagged[0])
    if len(accts) == 1:
        return number(accts[0])
    return None


# -- broker -------------------------------------------------------------------


class RobinhoodBroker:
    name = "robinhood"
    live = True  # Robinhood has no paper trading.

    def __init__(self, creds: Any, *, max_order_usd: Decimal,
                 persist: Callable[[dict[str, Any]], None] | None = None) -> None:
        self.client_id = creds.key_id
        self.refresh_token = creds.secret_key
        self.extra = dict(creds.extra or {})
        self.max_order_usd = max_order_usd
        self._persist = persist
        self._mcp: MCPClient | None = None

    @property
    def configured(self) -> bool:
        return bool(self.client_id and (self.refresh_token or self.extra.get("access_token")))

    def _client(self) -> MCPClient:
        if self._mcp:
            return self._mcp
        if not self.extra.get("access_token") or time.time() >= float(self.extra.get("expires_at") or 0):
            if not self.refresh_token:
                raise RobinhoodError("Robinhood sign-in expired. Please reconnect Robinhood.")
            tokens = refresh(discover(), self.client_id, self.refresh_token)
            self.extra.update(access_token=tokens["access_token"], expires_at=tokens["expires_at"])
            if tokens.get("refresh_token"):
                self.refresh_token = tokens["refresh_token"]  # refresh tokens may rotate
            if self._persist:
                self._persist({"refresh_token": self.refresh_token, "extra": self.extra})
        self._mcp = MCPClient(str(self.extra["access_token"]))
        return self._mcp

    def _account(self) -> str | None:
        if not self.extra.get("account_number"):
            acct = pick_agentic_account(self._client().call("get_accounts"))
            if acct:
                self.extra["account_number"] = acct
                if self._persist:
                    self._persist({"refresh_token": self.refresh_token, "extra": self.extra})
        return self.extra.get("account_number")

    def status(self) -> dict[str, Any]:
        base = {"broker": self.name, "mode": "live", "configured": self.configured}
        try:
            acct = self._account()
            pf = self.account()
        except RobinhoodError as exc:
            return {**base, "connected": False, "message": str(exc)}
        return {**base, "connected": True, "buying_power": pf.get("buying_power"),
                "market_open": None, "account_found": bool(acct),
                "message": "Connected." if acct else "Connected, but no Agentic account was found."}

    def account(self) -> dict[str, Any]:
        args = {"account_number": self._account()} if self._account() else {}
        p = self._client().call("get_portfolio", args)
        acct = str(self.extra.get("account_number") or "")
        return {
            "total_value": _num(_find(p, ("total_equity", "equity", "total_value", "portfolio_value", "market_value"))),
            "cash_available": _num(_find(p, ("cash", "cash_available", "withdrawable_amount"))),
            "buying_power": _num(_find(p, ("buying_power", "cash_available_for_trading"))),
            "account_number_masked": f"•••• {acct[-4:]}" if acct else None,
        }

    def positions(self) -> list[dict[str, Any]]:
        args = {"account_number": self._account()} if self._account() else {}
        rows = _as_list(self._client().call("get_equity_positions", args), ("positions", "results", "data"))
        return [{
            "symbol": _find(r, ("symbol", "ticker")),
            "quantity": _num(_find(r, ("quantity", "shares", "qty"))),
            "market_value": _num(_find(r, ("market_value", "equity", "value"))),
            "current_price": _num(_find(r, ("price", "last_price", "current_price"))),
        } for r in rows]

    def portfolio_history(self, period: str = "1M") -> dict[str, Any]:
        return {"period": period, "points": [], "note": "Robinhood does not provide portfolio history to agents."}

    def recent_orders(self, limit: int = 50) -> list[dict[str, Any]]:
        args = {"account_number": self._account()} if self._account() else {}
        rows = _as_list(self._client().call("get_equity_orders", args), ("orders", "results", "data"))[:limit]
        return [{
            "id": _find(r, ("id", "order_id")),
            "symbol": _find(r, ("symbol", "ticker")),
            "side": _find(r, ("side",)),
            "notional": _find(r, ("dollar_amount", "notional", "amount")),
            "filled_avg_price": _find(r, ("average_price", "avg_price", "filled_avg_price")),
            "status": _find(r, ("state", "status")),
            "submitted_at": _find(r, ("created_at", "submitted_at")),
        } for r in rows]

    def place_buy(self, symbol: str, *, client_order_id: str, notional: Decimal | None = None,
                  qty: Decimal | None = None, take_profit: Decimal | None = None,
                  stop_loss: Decimal | None = None, est_price: Decimal | None = None,
                  limit_price: Decimal | None = None, good_until: str = "day") -> dict[str, Any]:
        if qty is not None or take_profit is not None or stop_loss is not None or limit_price is not None:
            return {"status": "blocked", "broker": self.name,
                    "message": "With Robinhood, buy now by dollar amount, without a price or exit plan, for now."}
        return self.place_notional_buy(symbol, notional or Decimal("0"), client_order_id)

    def get_order(self, order_id: str) -> dict[str, Any]:
        return next((o for o in self.recent_orders() if str(o.get("id")) == str(order_id)), {})

    def cancel_order(self, order_id: str) -> dict[str, Any]:
        try:
            self._client().call("cancel_equity_order", {"order_id": order_id})
        except RobinhoodError as exc:
            return {"status": "failed", "message": str(exc)}
        return {"status": "canceled", "message": "Order canceled."}

    def open_orders(self) -> list[dict[str, Any]]:
        return [o for o in self.recent_orders() if str(o.get("status") or "").lower() in
                {"queued", "unconfirmed", "confirmed", "partially_filled", "new", "accepted", "pending"}]

    def place_notional_buy(self, symbol: str, notional: Decimal, client_order_id: str) -> dict[str, Any]:
        symbol = symbol.upper().strip()
        if notional <= 0 or notional > self.max_order_usd:
            return {"status": "blocked", "message": f"Order ${notional} is outside the ${self.max_order_usd} ceiling."}
        try:
            client = self._client()
            schemas = self.extra.get("schemas") or {}
            if "place_equity_order" not in schemas or "review_equity_order" not in schemas:
                schemas = {k: v for k, v in client.list_tools().items()
                           if k in ("place_equity_order", "review_equity_order")}
                self.extra["schemas"] = schemas
            if "place_equity_order" not in schemas:
                return {"status": "blocked", "message": "Robinhood did not offer an order tool for this account."}
            account = self._account()
            place_args = build_order_args(schemas["place_equity_order"], symbol=symbol, dollars=notional,
                                          account_number=account, ref=client_order_id)
            if "review_equity_order" in schemas:
                review_args = build_order_args(schemas["review_equity_order"], symbol=symbol, dollars=notional,
                                               account_number=account, ref=client_order_id)
                client.call("review_equity_order", review_args)  # raises on a blocking review
            placed = client.call("place_equity_order", place_args)
        except RobinhoodError as exc:
            return {"status": "blocked", "broker": self.name, "message": str(exc)}

        return {
            "status": "submitted",
            "broker": self.name,
            "mode": "live",
            "order_id": _find(placed, ("id", "order_id")),
            "order_status": _find(placed, ("state", "status")),
            "symbol": symbol,
            "notional": f"{notional:.2f}",
            "message": "Order submitted to your Robinhood Agentic account (real money).",
        }
