"""Auth for the product split, all driven by environment variables.

Three independent gates, deliberately kept separate:

* ``API_KEY`` guards the read-only JSON API (``/api/v1/*``). The Next.js server holds
  it and calls the backend server-side (``Authorization: Bearer <key>`` or
  ``X-API-Key``), so the key never reaches the browser. This is the "secure
  connection" between frontend and backend before real user accounts exist.
* ``ADMIN_API_KEY`` guards the mutating admin API (``/api/v1/admin/*``), which the
  frontend's admin panel drives. Separate from ``API_KEY`` so that holding the
  read key does not confer the ability to rewrite the engine's settings, and so the
  two rotate independently.
* ``ADMIN_USER`` / ``ADMIN_PASS`` gate the Jinja admin/testing UI with HTTP Basic.

If ``API_KEY`` or the Basic credentials are unset that gate is OPEN - so local
development (and the existing setup on the dev box) behaves exactly as before, while
the deployed host locks down simply by setting the vars. ``ADMIN_API_KEY`` is the
exception and fails CLOSED; see ``require_admin_key``.
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


def admin_api_key() -> str | None:
    return os.environ.get("ADMIN_API_KEY") or None


async def require_admin_key(request: Request) -> None:
    """FastAPI dependency: enforce ``ADMIN_API_KEY`` on the mutating admin API.

    Unlike ``require_api_key`` this FAILS CLOSED. An unset ``API_KEY`` only leaves a
    read-only score exposed, which is why it defaults to open for local dev; an unset
    ``ADMIN_API_KEY`` would let anyone on the internet rewrite the weights, the
    thresholds and the provider keys. "Not configured" therefore has to mean "refuse",
    not "allow" - a 503 on a dev box you forgot to configure is a nuisance, the
    alternative is a takeover.
    """
    expected = admin_api_key()
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Admin API is disabled: set ADMIN_API_KEY to enable it.",
        )
    got = _present_key(request)
    if not got or not secrets.compare_digest(got, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing admin API key",
            headers={"WWW-Authenticate": "Bearer"},
        )


def admin_key_warning() -> str | None:
    """Startup sanity check: an admin key equal to the read key defeats the split."""
    admin, read = admin_api_key(), api_key()
    if admin and read and secrets.compare_digest(admin, read):
        return ("ADMIN_API_KEY is identical to API_KEY - anything holding the read key "
                "can also write settings. Generate a separate value.")
    if admin and len(admin) < 24:
        return "ADMIN_API_KEY is short; use a long random value (32+ chars)."
    return None


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
