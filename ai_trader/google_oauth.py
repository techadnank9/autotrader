"""Sign in with Google: OAuth 2.0 authorization code flow with PKCE.

`state` defends against login CSRF and PKCE against code interception. Both live
in a short-lived signed cookie, so no server-side session store is needed. The
profile comes from Google's userinfo endpoint over TLS, and an email Google has
not verified is refused: accepting it would let anyone claim an account by
typing someone else's address.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from urllib.parse import urlencode

import httpx

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"


class GoogleAuthError(Exception):
    pass


def new_flow() -> tuple[str, str, str]:
    """Returns (state, code_verifier, code_challenge)."""
    state = secrets.token_urlsafe(24)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return state, verifier, challenge


def authorize_url(client_id: str, redirect_uri: str, state: str, challenge: str) -> str:
    return AUTH_URL + "?" + urlencode({
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "prompt": "select_account",
    })


def exchange(client_id: str, client_secret: str, redirect_uri: str, code: str, verifier: str) -> dict:
    try:
        tok = httpx.post(TOKEN_URL, data={
            "client_id": client_id, "client_secret": client_secret, "code": code,
            "code_verifier": verifier, "grant_type": "authorization_code", "redirect_uri": redirect_uri,
        }, timeout=15)
    except httpx.HTTPError as exc:
        raise GoogleAuthError("Could not reach Google.") from exc
    if tok.status_code != 200:
        raise GoogleAuthError("Google did not accept the sign-in code.")
    access = tok.json().get("access_token")
    if not access:
        raise GoogleAuthError("Google returned no access token.")
    try:
        info = httpx.get(USERINFO_URL, headers={"Authorization": f"Bearer {access}"}, timeout=15)
    except httpx.HTTPError as exc:
        raise GoogleAuthError("Could not read your Google profile.") from exc
    if info.status_code != 200:
        raise GoogleAuthError("Could not read your Google profile.")
    profile = info.json()
    if not profile.get("email") or profile.get("email_verified") is not True:
        raise GoogleAuthError("Your Google account's email is not verified.")
    return profile
