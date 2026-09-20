"""Postgres-backed stores.

Selected only when DATABASE_URL is set. Without it the app falls back to the
JSON file stores, so local development needs no database. Both implementations
expose the same interface, so DecisionService and the routes never know which
one they are talking to.
"""

from __future__ import annotations

import json
import time
from decimal import Decimal
from typing import Any

import psycopg
from psycopg.rows import dict_row

from ai_trader.accounts import (
    AccountError,
    DEMO_USER_ID,
    User,
    demo_user,
    hash_password,
    normalize_email,
    validate_signup,
    verify_password,
)
from ai_trader.decisions import Decision

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id    TEXT PRIMARY KEY,
    email      TEXT UNIQUE NOT NULL,
    password   TEXT NOT NULL,
    created_at DOUBLE PRECISION NOT NULL
);
CREATE TABLE IF NOT EXISTS decisions (
    decision_id TEXT PRIMARY KEY,
    status      TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    payload     JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS decisions_created_at_idx ON decisions (created_at DESC);
"""


class Database:
    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        self._ready = False

    def connect(self) -> psycopg.Connection:
        conn = psycopg.connect(self.dsn, row_factory=dict_row, connect_timeout=10)
        if not self._ready:
            with conn.cursor() as cur:
                cur.execute(SCHEMA)
            conn.commit()
            self._ready = True
        return conn


class PostgresUserStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    def create(self, email: str, password: str) -> User:
        validate_signup(email, password)
        email = normalize_email(email)
        import secrets

        user = User(user_id=f"usr_{secrets.token_hex(8)}", email=email, created_at=time.time())
        with self.db.connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1 FROM users WHERE email = %s", (email,))
            if cur.fetchone():
                raise AccountError("An account with that email already exists.")
            cur.execute(
                "INSERT INTO users (user_id, email, password, created_at) VALUES (%s, %s, %s, %s)",
                (user.user_id, email, hash_password(password), user.created_at),
            )
            conn.commit()
        return user

    def authenticate(self, email: str, password: str) -> User:
        with self.db.connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM users WHERE email = %s", (normalize_email(email),))
            row = cur.fetchone()
        if not row or not verify_password(password, str(row["password"])):
            raise AccountError("Email or password is incorrect.")
        return User(
            user_id=str(row["user_id"]), email=str(row["email"]), created_at=float(row["created_at"])
        )

    def get(self, user_id: str) -> User | None:
        if user_id == DEMO_USER_ID:
            return demo_user()
        with self.db.connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM users WHERE user_id = %s", (user_id,))
            row = cur.fetchone()
        if not row:
            return None
        return User(
            user_id=str(row["user_id"]), email=str(row["email"]), created_at=float(row["created_at"])
        )

    def count(self) -> int:
        with self.db.connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM users")
            row = cur.fetchone()
        return int(row["n"]) if row else 0


class PostgresDecisionStore:
    """Mirrors DecisionStore so DecisionService is unchanged."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def save(self, decision: Decision) -> Decision:
        with self.db.connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO decisions (decision_id, status, created_at, payload)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (decision_id)
                DO UPDATE SET status = EXCLUDED.status, payload = EXCLUDED.payload
                """,
                (decision.decision_id, decision.status, decision.created_at,
                 json.dumps(decision.to_dict())),
            )
            conn.commit()
        return decision

    def get(self, decision_id: str) -> Decision | None:
        with self.db.connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT payload FROM decisions WHERE decision_id = %s", (decision_id,))
            row = cur.fetchone()
        return Decision.from_dict(row["payload"]) if row else None

    def list(self, *, limit: int = 50) -> list[Decision]:
        with self.db.connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT payload FROM decisions ORDER BY created_at DESC LIMIT %s", (limit,)
            )
            rows = cur.fetchall()
        return [Decision.from_dict(r["payload"]) for r in rows]

    def open_decisions(self) -> list[Decision]:
        live: list[Decision] = []
        for decision in self.list():
            if decision.status != "pending":
                continue
            if decision.is_expired():
                decision.status = "expired"
                self.save(decision)
                continue
            live.append(decision)
        return live

    def create(self, **kwargs: Any) -> Decision:
        # Reuse the file store's construction logic, then persist to Postgres.
        from ai_trader.decisions import DecisionStore

        decision = DecisionStore.create(_Unsaved(), **kwargs)  # type: ignore[arg-type]
        return self.save(decision)


class _Unsaved:
    """Lets DecisionStore.create build a Decision without touching the filesystem."""

    def save(self, decision: Decision) -> Decision:
        return decision


def healthcheck(db: Database) -> dict[str, Any]:
    try:
        with db.connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1 AS ok")
            cur.fetchone()
        return {"connected": True}
    except Exception as exc:
        return {"connected": False, "error": str(exc)[:300]}
