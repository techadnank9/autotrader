"""Per-user trading preferences. Right now just the dollar amount for a one-tap buy."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class FileUserPrefs:
    def __init__(self, root: str | Path) -> None:
        self.path = Path(root) / "user_prefs.json"

    def _read(self) -> dict[str, Any]:
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def get(self, user_id: str) -> dict[str, Any]:
        return self._read().get(user_id) or {}

    def set_amount(self, user_id: str, amount: float) -> None:
        d = self._read()
        d.setdefault(user_id, {})["amount_usd"] = round(float(amount), 2)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(d, indent=2), encoding="utf-8")


class PostgresUserPrefs:
    SCHEMA = """
    CREATE TABLE IF NOT EXISTS user_prefs (
        user_id    TEXT PRIMARY KEY,
        amount_usd DOUBLE PRECISION
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

    def get(self, user_id: str) -> dict[str, Any]:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT amount_usd FROM user_prefs WHERE user_id = %s", (user_id,))
            row = cur.fetchone()
            return {"amount_usd": row["amount_usd"]} if row and row["amount_usd"] is not None else {}

    def set_amount(self, user_id: str, amount: float) -> None:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                """INSERT INTO user_prefs (user_id, amount_usd) VALUES (%s, %s)
                   ON CONFLICT (user_id) DO UPDATE SET amount_usd = EXCLUDED.amount_usd""",
                (user_id, round(float(amount), 2)))
