"""Prototype account store and signed sessions.

Deliberately dependency-free: scrypt from hashlib for password hashing, HMAC for
session cookies. This is enough to keep one person's dashboard separate from
another's. It is NOT production auth — there is no rate limiting, email
verification, password reset, or account recovery, and on a serverless host the
JSON store lives in an ephemeral filesystem.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCRYPT_N = 2 ** 14
SCRYPT_R = 8
SCRYPT_P = 1
DEMO_USER_ID = "demo"
SESSION_TTL_SECONDS = 60 * 60 * 24 * 14


class AccountError(Exception):
    """Raised for any signup or login failure the user should see."""


@dataclass(frozen=True)
class User:
    user_id: str
    email: str
    created_at: float
    is_demo: bool = False

    def to_public(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "email": self.email,
            "is_demo": self.is_demo,
            "created_at": self.created_at,
        }


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32
    )
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${_b64e(salt)}${_b64e(digest)}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, n, r, p, salt_b64, digest_b64 = encoded.split("$")
    except ValueError:
        return False
    if scheme != "scrypt":
        return False
    try:
        expected = _b64d(digest_b64)
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=_b64d(salt_b64),
            n=int(n), r=int(r), p=int(p),
            dklen=len(expected),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def validate_signup(email: str, password: str) -> None:
    email = normalize_email(email)
    if "@" not in email or "." not in email.split("@")[-1] or len(email) < 6:
        raise AccountError("Enter a valid email address.")
    if len(password) < 8:
        raise AccountError("Use at least 8 characters for your password.")
    if password.strip() == "":
        raise AccountError("Password cannot be blank.")


class UserStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.path = self.root / "users.json"

    def _read(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _write(self, data: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def create(self, email: str, password: str) -> User:
        validate_signup(email, password)
        email = normalize_email(email)
        data = self._read()
        if email in data:
            raise AccountError("An account with that email already exists.")
        user = User(user_id=f"usr_{secrets.token_hex(8)}", email=email, created_at=time.time())
        data[email] = {
            "user_id": user.user_id,
            "email": email,
            "created_at": user.created_at,
            "password": hash_password(password),
        }
        self._write(data)
        return user

    def authenticate(self, email: str, password: str) -> User:
        record = self._read().get(normalize_email(email))
        if not record or not verify_password(password, str(record.get("password", ""))):
            # Same message either way: do not reveal which accounts exist.
            raise AccountError("Email or password is incorrect.")
        return User(
            user_id=str(record["user_id"]),
            email=str(record["email"]),
            created_at=float(record.get("created_at", 0)),
        )

    def get(self, user_id: str) -> User | None:
        if user_id == DEMO_USER_ID:
            return demo_user()
        for record in self._read().values():
            if record.get("user_id") == user_id:
                return User(
                    user_id=str(record["user_id"]),
                    email=str(record["email"]),
                    created_at=float(record.get("created_at", 0)),
                )
        return None

    def count(self) -> int:
        return len(self._read())


def demo_user() -> User:
    return User(user_id=DEMO_USER_ID, email="demo@aitrader.local", created_at=0.0, is_demo=True)


class SessionSigner:
    """HMAC-signed `user_id.expiry.signature` cookie value."""

    def __init__(self, secret: str) -> None:
        self.secret = secret.encode("utf-8")

    def issue(self, user_id: str, *, ttl: int = SESSION_TTL_SECONDS) -> str:
        expires = int(time.time()) + ttl
        payload = f"{user_id}.{expires}"
        signature = hmac.new(self.secret, payload.encode("utf-8"), hashlib.sha256).hexdigest()
        return f"{payload}.{signature}"

    def verify(self, token: str | None) -> str | None:
        if not token:
            return None
        parts = token.rsplit(".", 1)
        if len(parts) != 2:
            return None
        payload, signature = parts
        expected = hmac.new(self.secret, payload.encode("utf-8"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            return None
        user_id, _, expires = payload.rpartition(".")
        try:
            if int(expires) < time.time():
                return None
        except ValueError:
            return None
        return user_id or None
