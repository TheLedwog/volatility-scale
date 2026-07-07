"""Auth for the product split, all driven by environment variables.

Two independent gates, deliberately kept separate:

* ``API_KEY`` guards the JSON API (``/api/v1/*``). The Next.js server holds it and
  calls the backend server-side (``Authorization: Bearer <key>`` or ``X-API-Key``),
  so the key never reaches the browser. This is the "secure connection" between
  frontend and backend before real user accounts exist.
* ``ADMIN_USER`` / ``ADMIN_PASS`` gate the Jinja admin/testing UI with HTTP Basic.

If the relevant env vars are unset the gate is OPEN - so local development (and the
existing setup on the dev box) behaves exactly as before, while the deployed host
locks down simply by setting the vars.
"""
from __future__ import annotations

import base64
import os
import secrets

from fastapi import HTTPException, Request, status


def api_key() -> str | None:
    return os.environ.get("API_KEY") or os.environ.get("TRADESCALE_API_KEY") or None


def _present_key(request: Request) -> str | None:
    auth = request.headers.get("Authorization", "")
    if auth[:7].lower() == "bearer ":
        return auth[7:].strip()
    return request.headers.get("X-API-Key")


async def require_api_key(request: Request) -> None:
    """FastAPI dependency: enforce the API key on the JSON API when one is configured."""
    expected = api_key()
    if not expected:  # unset -> open (local dev)
        return
    got = _present_key(request)
    if not got or not secrets.compare_digest(got, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key",
            headers={"WWW-Authenticate": "Bearer"},
        )


def admin_credentials() -> tuple[str, str] | None:
    user = os.environ.get("ADMIN_USER")
    pw = os.environ.get("ADMIN_PASS")
    return (user, pw) if (user and pw) else None


def check_basic_auth(header: str | None) -> bool:
    """Validate an ``Authorization: Basic`` header against ADMIN_USER/ADMIN_PASS.

    Returns True (allow) when no admin credentials are configured.
    """
    creds = admin_credentials()
    if not creds:  # unset -> open (local dev)
        return True
    if not header or header[:6].lower() != "basic ":
        return False
    try:
        raw = base64.b64decode(header[6:]).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return False
    user, _, pw = raw.partition(":")
    exp_user, exp_pw = creds
    # Compare both to avoid short-circuiting on the username.
    ok_user = secrets.compare_digest(user, exp_user)
    ok_pw = secrets.compare_digest(pw, exp_pw)
    return ok_user and ok_pw
