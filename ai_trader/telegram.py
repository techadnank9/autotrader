"""Telegram delivery for decision cards.

The card carries everything needed to decide without opening anything else, and
two inline buttons. Only the configured chat may answer: Telegram tells us who
pressed the button, and anyone else is rejected.
"""

from __future__ import annotations

import json
from html import escape
from typing import Any
from urllib import error, parse, request

from ai_trader.config import Settings
from ai_trader.decisions import APPROVED, Decision

API_ROOT = "https://api.telegram.org"


class TelegramNotConfigured(RuntimeError):
    pass


class TelegramClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    _me: dict[str, Any] | None = None
    _hooked: str | None = None

    @property
    def configured(self) -> bool:
        """A bot token is all that is needed: each user links their own chat."""
        return bool(self.settings.telegram_bot_token)

    def bot_username(self) -> str | None:
        if not self.configured:
            return None
        if self._me is None:
            me = self._call("getMe", {}, timeout=8)
            if not me.get("ok"):
                return None
            self._me = me["result"]
        return self._me.get("username")

    def send(self, chat_id: Any, text: str, rows: list[list[dict[str, str]]] | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                                   "disable_web_page_preview": True}
        if rows:
            payload["reply_markup"] = {"inline_keyboard": rows}
        return self._call("sendMessage", payload, timeout=8)

    def edit(self, chat_id: Any, message_id: Any, text: str,
             rows: list[list[dict[str, str]]] | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"chat_id": chat_id, "message_id": message_id, "text": text,
                                   "parse_mode": "HTML", "disable_web_page_preview": True,
                                   "reply_markup": {"inline_keyboard": rows or []}}
        return self._call("editMessageText", payload, timeout=8)

    def ensure_setup(self, url: str, secret: str) -> None:
        """Point the bot at this deployment once per process, and give it its menu."""
        if not self.configured or self._hooked == url:
            return
        info = self._call("getWebhookInfo", {}, timeout=8).get("result") or {}
        if info.get("url") != url or "message" not in (info.get("allowed_updates") or []):
            self._call("setWebhook", {"url": url, "secret_token": secret,
                                      "allowed_updates": ["message", "callback_query"]}, timeout=8)
            self._call("setMyCommands", {"commands": [
                {"command": "picks", "description": "Today's picks"},
                {"command": "stop", "description": "Stop alerts and disconnect"},
                {"command": "help", "description": "What this bot does"}]}, timeout=8)
            self._call("setMyDescription", {"description":
                "Today's stock picks and your order updates from AI Trader. "
                "Connect from the Alerts page in the app."}, timeout=8)
        self._hooked = url

    def _call(self, method: str, payload: dict[str, Any], *, timeout: int = 20) -> dict[str, Any]:
        if not self.settings.telegram_bot_token:
            raise TelegramNotConfigured("TELEGRAM_BOT_TOKEN is not set.")
        req = request.Request(
            f"{API_ROOT}/bot{self.settings.telegram_bot_token}/{method}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8", errors="replace"))
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:800]
            return {"ok": False, "error_code": exc.code, "description": detail}
        except (error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            return {"ok": False, "description": str(exc)}

    # -- outbound ------------------------------------------------------------

    def send_decision(self, decision: Decision) -> dict[str, Any]:
        result = self._call(
            "sendMessage",
            {
                "chat_id": self.settings.telegram_chat_id,
                "text": decision_card_text(decision),
                "parse_mode": "HTML",
                "reply_markup": {"inline_keyboard": decision_keyboard(decision)},
            },
        )
        return result

    def answer_callback(self, callback_id: str, text: str, *, alert: bool = False) -> dict[str, Any]:
        return self._call(
            "answerCallbackQuery",
            {"callback_query_id": callback_id, "text": text[:200], "show_alert": alert},
        )

    def settle_message(self, chat_id: Any, message_id: Any, decision: Decision) -> dict[str, Any]:
        """Replace the buttons with the outcome so the card cannot be tapped twice."""
        return self._call(
            "editMessageText",
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": settled_card_text(decision),
                "parse_mode": "HTML",
            },
        )

    def set_webhook(self, url: str) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "url": url,
            "allowed_updates": ["callback_query"],
            "drop_pending_updates": True,
        }
        if self.settings.telegram_webhook_secret:
            payload["secret_token"] = self.settings.telegram_webhook_secret
        return self._call("setWebhook", payload)

    def delete_webhook(self) -> dict[str, Any]:
        return self._call("deleteWebhook", {"drop_pending_updates": True})

    def status(self) -> dict[str, Any]:
        if not self.settings.telegram_bot_token:
            return {
                "configured": False,
                "chat_configured": bool(self.settings.telegram_chat_id),
                "secret_configured": bool(self.settings.telegram_webhook_secret),
                "message": "TELEGRAM_BOT_TOKEN is not set.",
            }
        me = self._call("getMe", {})
        info = self._call("getWebhookInfo", {})
        return {
            "configured": self.configured,
            "chat_configured": bool(self.settings.telegram_chat_id),
            "secret_configured": bool(self.settings.telegram_webhook_secret),
            "bot": (me.get("result") or {}).get("username") if me.get("ok") else None,
            "webhook_url": (info.get("result") or {}).get("url") if info.get("ok") else None,
            "last_error": (info.get("result") or {}).get("last_error_message") if info.get("ok") else None,
            "message": me.get("description") if not me.get("ok") else "Bot reachable.",
        }


# -- rendering ---------------------------------------------------------------


def decision_card_text(decision: Decision) -> str:
    confidence = f"{decision.confidence:.2f}"
    expires = decision.expires_at[11:16] if len(decision.expires_at) >= 16 else "--"
    return (
        f"<b>Today's call</b>\n\n"
        f"<b>{escape(decision.symbol)}</b> · {escape(decision.side)} "
        f"<b>${escape(decision.amount_usd)}</b>\n"
        f"Confidence {confidence}\n\n"
        f"{escape(decision.reason)}\n\n"
        f"<i>Expires {expires} UTC. No answer means no trade.</i>"
    )


def settled_card_text(decision: Decision) -> str:
    execution = decision.execution or {}
    outcome = {
        "approved": "Approved",
        "skipped": "Skipped",
        "expired": "Expired unanswered",
    }.get(decision.status, decision.status.title())

    lines = [
        f"<b>{escape(decision.symbol)}</b> · {escape(decision.side)} "
        f"<b>${escape(decision.amount_usd)}</b>",
        f"<b>{escape(outcome)}</b>",
    ]
    if decision.status == APPROVED:
        status = str(execution.get("status", "unknown"))
        lines.append(f"Execution: {escape(status)}")
        message = execution.get("message")
        if message:
            lines.append(escape(str(message)[:300]))
    return "\n".join(lines)


def decision_keyboard(decision: Decision) -> list[list[dict[str, str]]]:
    return [[
        {"text": "Skip", "callback_data": f"s:{decision.decision_id}"},
        {"text": "Approve", "callback_data": f"a:{decision.decision_id}"},
    ]]


def parse_callback(data: str) -> tuple[str, bool] | None:
    """`a:<id>` approves, `s:<id>` skips. Anything else is rejected."""
    if not isinstance(data, str) or ":" not in data:
        return None
    action, _, decision_id = data.partition(":")
    if action not in {"a", "s"} or not decision_id:
        return None
    return decision_id, action == "a"
