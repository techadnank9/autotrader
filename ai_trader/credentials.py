"""Per-user broker credentials, encrypted at rest.

Each user connects their own brokerage. The secret is encrypted with a
server-side key (Fernet: AES-128-CBC + HMAC-SHA256) before it touches storage,
and it is never returned to the browser: the API only ever exposes a masked key
id. With no encryption key configured the store refuses to save anything rather
than fall back to plaintext.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken


class CredentialError(Exception):
    """Raised for anything the user should see about saving or reading keys."""


@dataclass(frozen=True)
class BrokerCredentials:
    """For Alpaca: key id + secret. For Robinhood: OAuth client id + refresh token,
    with the access token, expiry, and discovered tool schemas in `extra`. Every
    field, `extra` included, is encrypted at rest."""

    provider: str
    key_id: str
    secret_key: str
    live: bool
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def masked_key_id(self) -> str:
        if self.provider == "robinhood":
            acct = str(self.extra.get("account_number") or "")
            return f"Agentic ••••{acct[-4:]}" if len(acct) >= 4 else "Agentic account"
        return f"{self.key_id[:4]}••••{self.key_id[-4:]}" if len(self.key_id) > 8 else "••••"


class Cipher:
    def __init__(self, key: str | None) -> None:
        self._fernet = None
        if key:
            try:
                self._fernet = Fernet(key.encode("utf-8"))
            except (ValueError, TypeError) as exc:
                raise CredentialError("CREDENTIALS_ENCRYPTION_KEY is not a valid Fernet key.") from exc

    @property
    def ready(self) -> bool:
        return self._fernet is not None

    def encrypt(self, payload: dict[str, Any]) -> str:
        if not self._fernet:
            raise CredentialError("Credential storage is not configured on this server.")
        return self._fernet.encrypt(json.dumps(payload).encode("utf-8")).decode("ascii")

    def decrypt(self, token: str) -> dict[str, Any]:
        if not self._fernet:
            raise CredentialError("Credential storage is not configured on this server.")
        try:
            return json.loads(self._fernet.decrypt(token.encode("ascii")))
        except (InvalidToken, ValueError) as exc:
            raise CredentialError("Stored credentials could not be decrypted.") from exc


def _record(creds: BrokerCredentials, cipher: Cipher) -> dict[str, Any]:
    return {
        "provider": creds.provider,
        "masked_key_id": creds.masked_key_id,
        "live": creds.live,
        "connected_at": time.time(),
        "ciphertext": cipher.encrypt(
            {"key_id": creds.key_id, "secret_key": creds.secret_key, "live": creds.live,
             "extra": creds.extra}
        ),
    }


def _decode(provider: str, row: dict[str, Any], cipher: Cipher) -> BrokerCredentials:
    data = cipher.decrypt(str(row["ciphertext"]))
    return BrokerCredentials(
        provider=provider, key_id=str(data["key_id"]),
        secret_key=str(data["secret_key"]), live=bool(data.get("live")),
        extra=data.get("extra") or {},
    )


class FileCredentialStore:
    def __init__(self, root: str | Path, cipher: Cipher) -> None:
        self.path = Path(root) / "credentials.json"
        self.cipher = cipher

    def _read(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def save(self, user_id: str, creds: BrokerCredentials) -> None:
        data = self._read()
        data[f"{user_id}:{creds.provider}"] = _record(creds, self.cipher)
        self._write(data)

    def get(self, user_id: str, provider: str) -> BrokerCredentials | None:
        row = self._read().get(f"{user_id}:{provider}")
        return _decode(provider, row, self.cipher) if row else None

    def summary(self, user_id: str, provider: str) -> dict[str, Any] | None:
        row = self._read().get(f"{user_id}:{provider}")
        if not row:
            return None
        return {"provider": provider, "masked_key_id": row["masked_key_id"],
                "live": row["live"], "connected_at": row["connected_at"]}

    def delete(self, user_id: str, provider: str) -> bool:
        data = self._read()
        existed = data.pop(f"{user_id}:{provider}", None) is not None
        self._write(data)
        return existed
