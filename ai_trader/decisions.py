"""Decision records: the only thing that can authorize an order.

A decision is proposed, delivered to the user, and answered exactly once. The
executor is unreachable except through a record whose status is `approved`, so a
lost message, a dead phone, or an expired window all fail closed.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

PENDING = "pending"
APPROVED = "approved"
SKIPPED = "skipped"
EXPIRED = "expired"

OPEN_STATES = {PENDING}


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Decision:
    decision_id: str
    symbol: str
    side: str
    amount_usd: str
    confidence: float
    reason: str
    evidence: list[str] = field(default_factory=list)
    created_at: str = ""
    expires_at: str = ""
    status: str = PENDING
    responded_at: str | None = None
    responder: str | None = None
    execution: dict[str, Any] | None = None
    delivery: dict[str, Any] = field(default_factory=dict)
    policy: dict[str, Any] = field(default_factory=dict)
    user_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "Decision":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in payload.items() if k in known})

    def is_expired(self, at: datetime | None = None) -> bool:
        if not self.expires_at:
            return False
        try:
            deadline = datetime.fromisoformat(self.expires_at)
        except ValueError:
            return False
        return (at or _now()) >= deadline

    def is_open(self) -> bool:
        return self.status in OPEN_STATES and not self.is_expired()


class DecisionStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def _path(self, decision_id: str) -> Path:
        return self.root / f"{decision_id}.json"

    def save(self, decision: Decision) -> Decision:
        self.root.mkdir(parents=True, exist_ok=True)
        self._path(decision.decision_id).write_text(
            json.dumps(decision.to_dict(), indent=2), encoding="utf-8"
        )
        return decision

    def get(self, decision_id: str) -> Decision | None:
        path = self._path(decision_id)
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        if not isinstance(payload, dict):
            return None
        return Decision.from_dict(payload)

    def list(self, *, user_id: str | None = None, limit: int = 50) -> list[Decision]:
        if not self.root.exists():
            return []
        decisions: list[Decision] = []
        for path in sorted(self.root.glob("*.json"), reverse=True):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if not isinstance(payload, dict):
                continue
            decision = Decision.from_dict(payload)
            if user_id is not None and decision.user_id != user_id:
                continue
            decisions.append(decision)
        decisions.sort(key=lambda d: d.created_at, reverse=True)
        return decisions[:limit]

    def open_decisions(self, *, user_id: str | None = None) -> list[Decision]:
        """Pending and not yet expired. Expiry is applied lazily on read."""
        live: list[Decision] = []
        for decision in self.list(user_id=user_id):
            if decision.status != PENDING:
                continue
            if decision.is_expired():
                decision.status = EXPIRED
                self.save(decision)
                continue
            live.append(decision)
        return live

    def create(
        self,
        *,
        symbol: str,
        side: str,
        amount_usd: Decimal,
        confidence: float,
        reason: str,
        evidence: list[str] | None = None,
        ttl_minutes: int = 240,
        policy: dict[str, Any] | None = None,
        user_id: str | None = None,
    ) -> Decision:
        created = _now()
        decision = Decision(
            decision_id=f"dec_{created.strftime('%Y%m%dT%H%M%S')}_{secrets.token_hex(3)}",
            symbol=symbol.upper(),
            side=side,
            amount_usd=f"{amount_usd:.2f}",
            confidence=round(float(confidence), 4),
            reason=reason,
            evidence=evidence or [],
            created_at=created.isoformat(),
            expires_at=(created + timedelta(minutes=ttl_minutes)).isoformat(),
            policy=policy or {},
            user_id=user_id,
        )
        return self.save(decision)


class DecisionClosed(Exception):
    """Raised when a decision can no longer be answered."""


class DecisionService:
    """Owns the approval gate.

    ``executor`` is called with the decision only after it has been approved by
    the authorized responder. Nothing else in the codebase may call it.
    """

    def __init__(
        self,
        store: DecisionStore,
        *,
        executor: Callable[[Decision], dict[str, Any]],
        max_open: int = 1,
    ) -> None:
        self.store = store
        self.executor = executor
        self.max_open = max_open

    def propose(self, *, user_id: str | None = None, enforce_open_limit: bool = True, **kwargs: Any) -> Decision:
        open_now = self.store.open_decisions(user_id=user_id) if enforce_open_limit else []
        if len(open_now) >= self.max_open:
            raise DecisionClosed(
                f"{len(open_now)} decision(s) already awaiting an answer; "
                "answer or let them expire before proposing another."
            )
        return self.store.create(user_id=user_id, **kwargs)

    def answer(
        self, decision_id: str, *, approved: bool, responder: str, user_id: str | None = None
    ) -> Decision:
        decision = self.store.get(decision_id)
        # Someone else's decision is reported exactly like a missing one, so ids
        # cannot be probed to learn what other users were proposed.
        if decision is None or (user_id is not None and decision.user_id != user_id):
            raise DecisionClosed("That decision no longer exists.")
        if decision.status != PENDING:
            raise DecisionClosed(f"That decision was already {decision.status}.")
        if decision.is_expired():
            decision.status = EXPIRED
            self.store.save(decision)
            raise DecisionClosed("That decision expired before it was answered.")

        decision.responded_at = _now().isoformat()
        decision.responder = responder
        decision.status = APPROVED if approved else SKIPPED
        self.store.save(decision)

        if not approved:
            return decision

        try:
            decision.execution = self.executor(decision)
        except Exception as exc:  # execution failures must not lose the record
            decision.execution = {"status": "failed", "message": str(exc)}
        return self.store.save(decision)
