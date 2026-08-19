"""The /api/v1/admin router: the settings surface, for the frontend's admin panel.

Everything the Jinja settings page can do, as JSON. Separated from the product API
because the shapes are different in kind: /api/v1/* is a read-only view of one shared
analysis that any signed-in user may see, while this mutates the engine for everyone
and is gated by its own key (ADMIN_API_KEY, see api/security.require_admin_key).

The contract is schema-driven on purpose. GET /settings returns the field schema
alongside the values, so the panel renders its controls from the response rather than
from a hard-coded form; adding a setting is then one entry in service/settings_schema
and no frontend release. The same table validates the patch, so the bounds the UI
shows are the bounds the API enforces.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field

from ..scheduler import scheduler_status
from ..service import jobs
from ..service.settings_service import (
    SettingsError,
    apply_patch,
    clear_secret,
    reset_settings,
    set_secret,
    settings_payload,
    test_secret,
)
from .security import require_admin_key

router = APIRouter(prefix="/api/v1/admin", tags=["admin"],
                   dependencies=[Depends(require_admin_key)])


class PatchRequest(BaseModel):
    patch: dict = Field(..., description="Nested subset of the config to change, e.g. "
                                         '{"thresholds": {"good": 70}}')


class ResetRequest(BaseModel):
    section: str | None = Field(
        None, description="Config section to reset. Omit to reset EVERYTHING to defaults.")


class SecretRequest(BaseModel):
    value: str = Field(..., description="The credential. Write-only: never read back.")


class SecretTestRequest(BaseModel):
    value: str | None = Field(
        None, description="Key to test. Omit to test whichever key is currently active.")


def _bad_request(exc: SettingsError):
    """422 with per-field messages, so the panel can mark the offending control."""
    # Literal 422: Starlette renamed its constant (ENTITY -> CONTENT) and the old
    # spelling now warns, so don't couple to either name.
    return HTTPException(status_code=422,
                         detail={"message": "Validation failed", "errors": exc.errors})


# ── settings ────────────────────────────────────────────────────────────────────

@router.get("/settings")
def get_settings():
    """Current values, the defaults, and the schema to render them from.

    `config` never contains credentials - they are removed, not blanked, so a
    round-trip cannot clear a working key. Their state is in `meta.secrets`.
    """
    return settings_payload()


@router.put("/settings")
def put_settings(body: PatchRequest):
    """Apply a partial change.

    Send only what changed. Unknown paths are rejected rather than ignored, so a
    typo can't look saved. Structural sections (session/tickers/gate/ml) must be sent
    whole. On success the response reports which paths actually moved and what that
    implies - see `effects`:

    * `scheduler_restarted` - the cron triggers were rebuilt, so a schedule change is
      already live and needs no redeploy.
    * `requires_regrade` - a label cutoff moved, so stored sessions are now graded on
      a mix of old and new definitions. POST /actions/regrade to fix.
    * `prediction_stale` - today's frozen verdict predates this change. It is NOT
      recomputed automatically; POST /actions/predict to recut it.
    """
    try:
        return apply_patch(body.patch)
    except SettingsError as exc:
        raise _bad_request(exc) from exc


@router.post("/settings/reset")
def post_reset(body: ResetRequest | None = None):
    """Drop stored overrides for one section, or all of them."""
    try:
        return reset_settings(body.section if body else None)
    except SettingsError as exc:
        raise _bad_request(exc) from exc


# ── credentials ─────────────────────────────────────────────────────────────────

@router.post("/secrets/{name}")
def post_secret(name: str, body: SecretRequest):
    """Store a credential (`openai` | `webz`). Write-only - it is never returned.

    Note the env var of the same name still wins if one is set; the response says so
    in `warnings` rather than letting a saved key look active when it isn't.
    """
    try:
        return set_secret(name, body.value)
    except SettingsError as exc:
        raise _bad_request(exc) from exc


@router.delete("/secrets/{name}")
def delete_secret(name: str):
    """Remove the stored credential. Any env var stays in force."""
    try:
        return clear_secret(name)
    except SettingsError as exc:
        raise _bad_request(exc) from exc


@router.post("/secrets/{name}/test")
def post_secret_test(name: str, body: SecretTestRequest | None = None):
    """Authenticate against the provider. Costs nothing - no completion is requested."""
    try:
        return test_secret(name, body.value if body else None)
    except SettingsError as exc:
        raise _bad_request(exc) from exc


# ── actions ─────────────────────────────────────────────────────────────────────

@router.get("/actions")
def list_actions():
    """The action catalogue plus each one's last outcome, for rendering the buttons."""
    return {"actions": jobs.actions_payload(), "scheduler": scheduler_status()}


@router.post("/actions/{action}", status_code=status.HTTP_202_ACCEPTED)
def post_action(action: str, response: Response):
    """Start an action in the background and return a job to poll.

    202 on a fresh start, 200 when that action was already running (in which case the
    in-flight job comes back rather than a second one being started).
    """
    try:
        job = jobs.start(action)
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown action {action!r} - expected one of: "
                   f"{', '.join(sorted(jobs.ACTIONS))}",
        ) from exc
    if job.get("already_running"):
        response.status_code = status.HTTP_200_OK
    return job


@router.get("/actions/jobs/{job_id}")
def get_job(job_id: str):
    """Poll a job. `status` is running | ok | error."""
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Unknown or expired job id")
    return job
