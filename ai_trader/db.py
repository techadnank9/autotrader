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
ALTER TABLE decisions ADD COLUMN IF NOT EXISTS user_id TEXT;
CREATE INDEX IF NOT EXISTS decisions_user_idx ON decisions (user_id, created_at DESC);
CREATE TABLE IF NOT EXISTS research_runs (
    run_id     TEXT PRIMARY KEY,
    created_at DOUBLE PRECISION NOT NULL,
    payload    JSONB NOT NULL
);
CREATE TABLE IF NOT EXISTS broker_credentials (
    user_id       TEXT NOT NULL,
    provider      TEXT NOT NULL,
    masked_key_id TEXT NOT NULL,
    live          BOOLEAN NOT NULL,
    connected_at  DOUBLE PRECISION NOT NULL,
    ciphertext    TEXT NOT NULL,
    PRIMARY KEY (user_id, provider)
);
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

    def get_or_create_verified(self, email: str, provider: str) -> User:
        import secrets

        email = normalize_email(email)
        with self.db.connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM users WHERE email = %s", (email,))
            row = cur.fetchone()
            if row:
                return User(user_id=str(row["user_id"]), email=str(row["email"]),
                            created_at=float(row["created_at"]))
            user = User(user_id=f"usr_{secrets.token_hex(8)}", email=email, created_at=time.time())
            cur.execute(
                "INSERT INTO users (user_id, email, password, created_at) VALUES (%s, %s, %s, %s)",
                (user.user_id, email, f"oauth${provider}", user.created_at),
            )
            conn.commit()
        return user

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
                INSERT INTO decisions (decision_id, status, created_at, payload, user_id)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (decision_id)
                DO UPDATE SET status = EXCLUDED.status, payload = EXCLUDED.payload
                """,
                (decision.decision_id, decision.status, decision.created_at,
                 json.dumps(decision.to_dict()), decision.user_id),
            )
            conn.commit()
        return decision

    def get(self, decision_id: str) -> Decision | None:
        with self.db.connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT payload FROM decisions WHERE decision_id = %s", (decision_id,))
            row = cur.fetchone()
        return Decision.from_dict(row["payload"]) if row else None

    def list(self, *, user_id: str | None = None, limit: int = 50) -> list[Decision]:
        with self.db.connect() as conn, conn.cursor() as cur:
            if user_id is None:
                cur.execute("SELECT payload FROM decisions ORDER BY created_at DESC LIMIT %s", (limit,))
            else:
                cur.execute(
                    "SELECT payload FROM decisions WHERE user_id = %s ORDER BY created_at DESC LIMIT %s",
                    (user_id, limit),
                )
            rows = cur.fetchall()
        return [Decision.from_dict(r["payload"]) for r in rows]

    def open_decisions(self, *, user_id: str | None = None) -> list[Decision]:
        live: list[Decision] = []
        for decision in self.list(user_id=user_id):
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


class PostgresCredentialStore:
    """Same interface as FileCredentialStore; ciphertext only, never the secret."""

    def __init__(self, db: Database, cipher: Any) -> None:
        self.db = db
        self.cipher = cipher

    def save(self, user_id: str, creds: Any) -> None:
        from ai_trader.credentials import _record

        rec = _record(creds, self.cipher)
        with self.db.connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO broker_credentials (user_id, provider, masked_key_id, live, connected_at, ciphertext)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (user_id, provider) DO UPDATE SET
                  masked_key_id = EXCLUDED.masked_key_id, live = EXCLUDED.live,
                  connected_at = EXCLUDED.connected_at, ciphertext = EXCLUDED.ciphertext
                """,
                (user_id, rec["provider"], rec["masked_key_id"], rec["live"], rec["connected_at"], rec["ciphertext"]),
            )
            conn.commit()

    def _row(self, user_id: str, provider: str) -> dict[str, Any] | None:
        with self.db.connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM broker_credentials WHERE user_id = %s AND provider = %s", (user_id, provider)
            )
            return cur.fetchone()

    def get(self, user_id: str, provider: str) -> Any:
        from ai_trader.credentials import _decode

        row = self._row(user_id, provider)
        return _decode(provider, row, self.cipher) if row else None

    def summary(self, user_id: str, provider: str) -> dict[str, Any] | None:
        row = self._row(user_id, provider)
        if not row:
            return None
        return {"provider": provider, "masked_key_id": row["masked_key_id"],
                "live": bool(row["live"]), "connected_at": float(row["connected_at"])}

    def delete(self, user_id: str, provider: str) -> bool:
        with self.db.connect() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM broker_credentials WHERE user_id = %s AND provider = %s", (user_id, provider))
            deleted = cur.rowcount > 0
            conn.commit()
        return deleted


class PostgresPicksStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    def latest(self) -> dict[str, Any] | None:
        with self.db.connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT payload FROM research_runs ORDER BY created_at DESC LIMIT 1")
            row = cur.fetchone()
        return row["payload"] if row else None

    def save(self, run: dict[str, Any]) -> None:
        with self.db.connect() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO research_runs (run_id, created_at, payload) VALUES (%s, %s, %s) "
                "ON CONFLICT (run_id) DO UPDATE SET payload = EXCLUDED.payload",
                (run["run_id"], run["created_at"], json.dumps(run)),
            )
            conn.commit()
