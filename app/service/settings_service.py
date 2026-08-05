"""Read, validate and apply engine settings for the admin API.

The Jinja settings page writes config straight from a form, falling back to the old
value whenever a field won't parse (app/main.py). That is forgiving enough for a
page you drive by hand, but an API needs the opposite: a bad value has to come back
as an error naming the field, and an unrecognised path has to be refused outright -
a typo that silently writes `thresholds.gud` leaves a setting that looks saved,
reads back, and does nothing.

What it does beyond storing values:

* Secrets are neither returned nor patchable here. They have write-only endpoints,
  so a GET can't leak one and a round-tripped form can't blank one.
* Whole structural sections (session/tickers/gate/ml) must arrive complete, so a
  partial patch can't silently drop `session.close`.
* Every save reports its EFFECTS. Saving from a frontend never restarts the process,
  so without this a schedule change would look saved and quietly do nothing until the
  next redeploy, and moving a label cutoff would leave the stored history graded on
  two different bases at once.
"""
from __future__ import annotations

import copy
import json
import re
from typing import Any

from ..config import (
    DEFAULTS,
    get_config,
    openai_key_status,
    reset as reset_all_config,
    set_section,
)
from ..timeutils import parse_hhmm, today_et
from .settings_schema import (
    SECRET_PATHS,
    Field,
    all_fields,
    field_map,
    sections_payload,
)

# Sections edited wholesale as JSON. A patch must supply the complete object.
JSON_SECTIONS = ("session", "tickers", "gate", "ml")

_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


class SettingsError(Exception):
    """One or more field-level validation failures.

    Carries a list of {"path", "message"} so the panel can highlight the offending
    control instead of showing a single opaque string.
    """

    def __init__(self, errors: list[dict]):
        self.errors = errors
        super().__init__("; ".join(f"{e['path']}: {e['message']}" for e in errors))


# ── payload ─────────────────────────────────────────────────────────────────────

def public_config(cfg: dict) -> dict:
    """The config with every credential removed (not blanked - removed).

    Blanking to "" would be worse than useless: the frontend would round-trip the
    empty string back and clear a working key.
    """
    out = copy.deepcopy(cfg)
    for path in SECRET_PATHS:
        section, _, leaf = path.partition(".")
        if isinstance(out.get(section), dict):
            out[section].pop(leaf, None)
    return out


def secret_status(cfg: dict) -> dict:
    """Where each credential comes from, without revealing any of it."""
    import os

    def block(env_var: str, config_path: str) -> dict:
        section, _, leaf = config_path.partition(".")
        env_present = bool(os.environ.get(env_var))
        stored_present = bool((cfg.get(section) or {}).get(leaf))
        return {
            "env_var": env_var,
            "env_present": env_present,
            "stored_present": stored_present,
            # The env var wins in config.openai_api_key()/webz_api_key(), so a stored
            # key is inert while one is set - the panel needs to say so rather than
            # show a key that isn't being used.
            "source": "environment" if env_present else "stored" if stored_present else "none",
            "active": env_present or stored_present,
        }

    return {
        "openai": {**block("OPENAI_API_KEY", "providers.openai_api_key"),
                   **openai_key_status(cfg)},
        "webz": block("WEBZ_API_KEY", "providers.webz_api_key"),
    }


def settings_payload(cfg: dict | None = None) -> dict:
    """Everything the settings screen needs in one call."""
    cfg = cfg or get_config()
    from .calendar_view import calendar_status

    fields = all_fields(cfg)
    return {
        "config": public_config(cfg),
        "defaults": public_config(DEFAULTS),
        "sections": sections_payload(),
        "schema": [f.to_dict() for f in fields],
        "meta": {
            "secrets": secret_status(cfg),
            "calendar": calendar_status(cfg),
            "json_sections": list(JSON_SECTIONS),
        },
    }


# ── path helpers ────────────────────────────────────────────────────────────────

def _get_path(cfg: dict, path: str) -> Any:
    node: Any = cfg
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _set_path(cfg: dict, path: str, value: Any) -> None:
    parts = path.split(".")
    node = cfg
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value


# ── coercion ────────────────────────────────────────────────────────────────────

def _coerce(field: Field, value: Any, errors: list[dict]) -> Any:
    """Validate + normalise one value, appending to `errors` and returning None on
    failure (the caller drops failed paths, so one bad field can't corrupt the rest)."""
    path = field.path

    def fail(msg: str):
        errors.append({"path": path, "message": msg})
        return None

    if field.type in ("int", "float"):
        if isinstance(value, bool):  # bool is an int subclass; almost never intended
            return fail("expected a number, got a boolean")
        try:
            num = int(value) if field.type == "int" else float(value)
        except (TypeError, ValueError):
            return fail(f"expected {'an integer' if field.type == 'int' else 'a number'}")
        if field.type == "int" and isinstance(value, float) and value != int(value):
            return fail("expected a whole number")
        if field.min is not None and num < field.min:
            return fail(f"must be at least {field.min:g}")
        if field.max is not None and num > field.max:
            return fail(f"must be at most {field.max:g}")
        return num

    if field.type == "bool":
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip().lower() in ("true", "false", "1", "0", "on", "off"):
            return value.strip().lower() in ("true", "1", "on")
        if value in (0, 1):
            return bool(value)
        return fail("expected true or false")

    if field.type in ("str", "text"):
        if not isinstance(value, str):
            return fail("expected text")
        text = value.strip()
        if not text:
            return fail("must not be empty")
        return text

    if field.type == "time":
        if not isinstance(value, str) or not _TIME_RE.match(value.strip()):
            return fail("expected a 24-hour time as HH:MM")
        t = parse_hhmm(value.strip())
        return f"{t.hour:02d}:{t.minute:02d}"

    if field.type == "enum":
        if value not in (field.choices or ()):
            return fail(f"must be one of: {', '.join(field.choices or ())}")
        return value

    if field.type == "json":
        return _coerce_json_section(field, value, errors)

    return fail(f"unsupported field type {field.type!r}")


def _coerce_json_section(field: Field, value: Any, errors: list[dict]) -> Any:
    """A whole structural section. Must be complete and structurally sound."""
    path = field.path

    def fail(msg: str, sub: str = ""):
        errors.append({"path": f"{path}.{sub}" if sub else path, "message": msg})
        return None

    if isinstance(value, str):  # a raw textarea, as the Jinja page sends it
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            return fail(f"invalid JSON: {exc}")
    if not isinstance(value, dict):
        return fail("expected a JSON object")

    default = DEFAULTS.get(path, {})
    missing = [k for k in default if k not in value]
    if missing:
        # Merging a partial object would silently keep stale values for the omitted
        # keys; refusing makes the caller send what it means.
        return fail(f"missing key(s): {', '.join(sorted(missing))} - send the whole section")
    unknown = [k for k in value if k not in default]
    if unknown:
        return fail(f"unknown key(s): {', '.join(sorted(unknown))}")

    checker = _STRUCT_CHECKS.get(path)
    if checker:
        before = len(errors)
        checker(value, fail)
        if len(errors) > before:
            return None
    return value


# ── structural checks for the raw-JSON sections ─────────────────────────────────

def _check_session(v: dict, fail) -> None:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    try:
        ZoneInfo(str(v.get("tz")))
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        fail(f"unknown timezone {v.get('tz')!r}", "tz")
    for key in ("open", "close"):
        if not isinstance(v.get(key), str) or not _TIME_RE.match(v[key].strip()):
            fail("expected a 24-hour time as HH:MM", key)
    if isinstance(v.get("open"), str) and isinstance(v.get("close"), str):
        if _TIME_RE.match(v["open"].strip()) and _TIME_RE.match(v["close"].strip()):
            if parse_hhmm(v["open"]) >= parse_hhmm(v["close"]):
                fail("the session must open before it closes", "open")


def _check_tickers(v: dict, fail) -> None:
    for key, val in v.items():
        if not isinstance(val, str) or not val.strip():
            fail("expected a non-empty ticker symbol", key)


def _check_gate(v: dict, fail) -> None:
    for key in ("veto_categories", "warn_categories"):
        val = v.get(key)
        if not isinstance(val, list) or not all(isinstance(x, str) and x.strip() for x in val):
            fail("expected a list of category names", key)
    veto, warn = v.get("veto_categories"), v.get("warn_categories")
    if isinstance(veto, list) and isinstance(warn, list):
        both = sorted(set(veto) & set(warn))
        if both:
            # decide_gate checks veto first, so a category in both silently never warns.
            fail(f"category in both veto and warn: {', '.join(both)}", "warn_categories")
    if v.get("min_impact") not in ("High", "Medium", "Low"):
        fail("expected High, Medium or Low", "min_impact")
    buf = v.get("session_buffer_min")
    if isinstance(buf, bool) or not isinstance(buf, int) or buf < 0:
        fail("expected a whole number of minutes (0 or more)", "session_buffer_min")


def _check_ml(v: dict, fail) -> None:
    for key in ("lookback_days_daily", "lookback_days_hourly", "min_session_bars"):
        val = v.get(key)
        if isinstance(val, bool) or not isinstance(val, int) or val <= 0:
            fail("expected a positive whole number", key)
    if not isinstance(v.get("model_file"), str) or not v["model_file"].strip():
        fail("expected a filename", "model_file")
    frac = v.get("test_fraction")
    if not isinstance(frac, (int, float)) or isinstance(frac, bool) or not 0 < frac < 1:
        fail("expected a fraction between 0 and 1 (exclusive)", "test_fraction")
    for key in ("min_spearman", "min_lift"):
        val = v.get(key)
        if isinstance(val, bool) or not isinstance(val, (int, float)):
            fail("expected a number", key)


_STRUCT_CHECKS = {
    "session": _check_session,
    "tickers": _check_tickers,
    "gate": _check_gate,
    "ml": _check_ml,
}


# ── cross-field rules ───────────────────────────────────────────────────────────

def _cross_check(candidate: dict, touched: set[str], errors: list[dict]) -> None:
    """Rules that only make sense against the whole merged config.

    Only reported against paths the caller actually touched, so a patch isn't
    rejected for a pre-existing inconsistency it had no part in.
    """
    def report(path: str, msg: str, related: tuple[str, ...]) -> None:
        if touched & set(related):
            errors.append({"path": path, "message": msg})

    thr = candidate.get("thresholds", {})
    good, caution = thr.get("good"), thr.get("caution")
    if isinstance(good, int) and isinstance(caution, int) and good <= caution:
        report("thresholds.good",
               f"must be above the caution line ({caution}) - otherwise there is no "
               "mixed band and every day is either good or choppy",
               ("thresholds.good", "thresholds.caution"))

    hi, lo = thr.get("label_directional_er"), thr.get("label_choppy_er")
    if isinstance(hi, (int, float)) and isinstance(lo, (int, float)) and hi <= lo:
        report("thresholds.label_directional_er",
               f"must be above the choppy cutoff ({lo:g})",
               ("thresholds.label_directional_er", "thresholds.label_choppy_er"))

    weights = candidate.get("weights", {})
    if any(p.startswith("weights.") for p in touched):
        total = sum(v for v in weights.values() if isinstance(v, (int, float)))
        if total <= 0:
            errors.append({"path": "weights",
                           "message": "at least one weight must be above zero - the "
                                      "factors are normalised by their total"})


# ── applying a patch ────────────────────────────────────────────────────────────

def _collect(patch: dict, fmap: dict[str, Field], prefix: str,
             errors: list[dict]) -> list[tuple[Field, Any]]:
    """Walk the incoming patch down to known leaves."""
    found: list[tuple[Field, Any]] = []
    for key, value in patch.items():
        path = f"{prefix}{key}"
        if path in SECRET_PATHS:
            errors.append({"path": path,
                           "message": "credentials are not editable here - use the "
                                      "/api/v1/admin/secrets endpoints"})
            continue
        field = fmap.get(path)
        if field is not None:
            found.append((field, value))
        elif isinstance(value, dict) and value:
            found.extend(_collect(value, fmap, path + ".", errors))
        else:
            errors.append({"path": path,
                           "message": "no settings supplied for this section"
                                      if isinstance(value, dict) else "unknown setting"})
    return found


def apply_patch(patch: dict) -> dict:
    """Validate `patch`, persist what changed, and report the consequences.

    Raises SettingsError with per-field messages if anything fails validation;
    nothing is written in that case.
    """
    if not isinstance(patch, dict) or not patch:
        raise SettingsError([{"path": "patch", "message": "expected a non-empty object"}])

    cfg = get_config()
    fmap = field_map(cfg)
    errors: list[dict] = []

    leaves = _collect(patch, fmap, "", errors)
    candidate = copy.deepcopy(cfg)
    touched: set[str] = set()
    for field, raw in leaves:
        coerced = _coerce(field, raw, errors)
        if coerced is None:
            continue
        touched.add(field.path)
        _set_path(candidate, field.path, coerced)

    _cross_check(candidate, touched, errors)
    if errors:
        raise SettingsError(errors)

    changed = sorted(p for p in touched if _get_path(cfg, p) != _get_path(candidate, p))
    for section in {p.split(".")[0] for p in changed}:
        set_section(section, candidate[section])

    return {
        "ok": True,
        "changed": changed,
        "config": public_config(get_config()),
        "effects": _apply_effects(changed, candidate),
        "warnings": _warnings(candidate),
    }


def reset_settings(section: str | None = None) -> dict:
    """Drop stored overrides - one section, or all of them.

    Effects are derived by diffing the config either side of the reset, not assumed
    from the section names: resetting `thresholds` when the label cutoffs were already
    at their defaults should not tell you a re-grade is needed.
    """
    before = get_config()
    if section is None:
        reset_all_config()
        sections = sorted(DEFAULTS.keys())
    else:
        if section not in DEFAULTS:
            raise SettingsError([{"path": "section", "message": f"unknown section {section!r}"}])
        set_section(section, copy.deepcopy(DEFAULTS[section]))
        sections = [section]

    cfg = get_config()
    changed = sorted(p for p in field_map(cfg)
                     if _get_path(before, p) != _get_path(cfg, p))
    return {
        "ok": True,
        "reset": sections,
        "changed": changed,
        "config": public_config(cfg),
        "effects": _apply_effects(changed, cfg),
        "warnings": _warnings(cfg),
    }


# ── effects ─────────────────────────────────────────────────────────────────────

_PREDICTION_INPUTS = ("weights.", "scoring.", "news.", "thresholds.good",
                      "thresholds.caution", "thresholds.dead_day_range_pct",
                      "gate", "tickers", "session")


def _touches(changed: list[str], prefixes: tuple[str, ...]) -> bool:
    return any(c.startswith(p) or c == p.rstrip(".") for c in changed for p in prefixes)


def _apply_effects(changed: list[str], cfg: dict) -> dict:
    """Make the save actually take effect, and report what the caller must still do."""
    effects = {
        "scheduler_restarted": False,
        "scheduler_error": None,
        "requires_regrade": False,
        "prediction_stale": False,
    }
    if not changed:
        return effects

    # The scheduler builds its cron triggers once at startup, so without this a
    # schedule change would sit in the DB doing nothing until the next redeploy.
    if _touches(changed, ("schedule.",)):
        try:
            from ..scheduler import restart_scheduler
            restart_scheduler()
            effects["scheduler_restarted"] = True
        except Exception as exc:  # noqa: BLE001 - a failed re-arm must not fail the save
            effects["scheduler_error"] = " ".join(str(exc).split())[:200]

    # Moving a label cutoff re-bases every stored label; until a re-grade runs the
    # track record is measuring two different definitions at once.
    if _touches(changed, ("thresholds.label_directional_er", "thresholds.label_choppy_er")):
        effects["requires_regrade"] = True

    # Today's verdict was frozen at the open and is not recomputed on save.
    if _touches(changed, _PREDICTION_INPUTS):
        from ..store import prediction_for
        effects["prediction_stale"] = prediction_for(today_et().isoformat()) is not None

    return effects


# ── credentials ─────────────────────────────────────────────────────────────────
# Write-only: set, clear, test. Nothing here ever returns a stored key.

SECRETS = {
    "openai": {"path": "providers.openai_api_key", "env_var": "OPENAI_API_KEY"},
    "webz": {"path": "providers.webz_api_key", "env_var": "WEBZ_API_KEY"},
}


def _secret_spec(name: str) -> dict:
    spec = SECRETS.get(name)
    if not spec:
        raise SettingsError([{"path": "name",
                              "message": f"unknown credential {name!r} - expected one of: "
                                         f"{', '.join(sorted(SECRETS))}"}])
    return spec


def set_secret(name: str, value: str) -> dict:
    spec = _secret_spec(name)
    value = (value or "").strip()
    if not value:
        raise SettingsError([{"path": "value", "message": "no key supplied"}])
    cfg = get_config()
    section, _, leaf = spec["path"].partition(".")
    set_section(section, {**cfg[section], leaf: value})
    cfg = get_config()
    status = secret_status(cfg)[name]
    warnings = []
    if status["env_present"]:
        # config.openai_api_key()/webz_api_key() read the env var first, so the key
        # just saved is inert until that var is unset. Silence here would look like
        # the save didn't work.
        warnings.append(f"{spec['env_var']} is set in the environment and takes "
                        "precedence, so this stored key will not be used until it is unset.")
    return {"ok": True, "name": name, "status": status, "warnings": warnings}


def clear_secret(name: str) -> dict:
    spec = _secret_spec(name)
    cfg = get_config()
    section, _, leaf = spec["path"].partition(".")
    set_section(section, {**cfg[section], leaf: ""})
    return {"ok": True, "name": name, "status": secret_status(get_config())[name]}


def _test_openai(key: str) -> tuple[bool, str]:
    """A no-cost auth check: list models."""
    try:
        from openai import OpenAI

        OpenAI(api_key=key, timeout=12.0, max_retries=0).models.list()
        return True, "Key works - OpenAI authenticated."
    except Exception as exc:  # noqa: BLE001
        return False, f"Key failed: {' '.join(str(exc).split())[:200]}"


def _test_webz(key: str) -> tuple[bool, str]:
    """Cheapest possible authenticated call: one result, throwaway query."""
    try:
        import requests

        from ..providers.news_webz import WEBZ_URL

        r = requests.get(
            WEBZ_URL,
            params={"token": key, "q": "language:english", "size": 1, "format": "json"},
            timeout=15,
        )
        if r.status_code == 401:
            return False, "Key rejected (401) - check the token."
        r.raise_for_status()
        left = r.json().get("requestsLeft")
        suffix = f" {left} requests left this month." if left is not None else ""
        return True, f"Key works - Webz authenticated.{suffix}"
    except Exception as exc:  # noqa: BLE001
        return False, f"Key failed: {' '.join(str(exc).split())[:200]}"


def test_secret(name: str, value: str | None = None) -> dict:
    """Test a supplied key, or - when none is given - whatever key is actually active."""
    spec = _secret_spec(name)
    key = (value or "").strip()
    source = "supplied"
    if not key:
        from ..config import openai_api_key, webz_api_key

        cfg = get_config()
        key = (openai_api_key(cfg) if name == "openai" else webz_api_key(cfg))
        source = secret_status(cfg)[name]["source"]
    if not key:
        return {"ok": False, "name": name, "source": "none",
                "message": f"No key to test - save one, or set {spec['env_var']}."}
    ok, message = (_test_openai(key) if name == "openai" else _test_webz(key))
    return {"ok": ok, "name": name, "source": source, "message": message}


def _warnings(cfg: dict) -> list[str]:
    """Non-blocking notes about a configuration that will run but not as intended."""
    out: list[str] = []
    status = secret_status(cfg)
    if cfg.get("news", {}).get("enabled") and not status["openai"]["active"]:
        out.append("News is enabled but no OpenAI key is set: headlines will be "
                   "fetched and left unscored, so the news factor stays blind.")
    if not cfg.get("schedule", {}).get("enabled", True):
        out.append("Scheduled jobs are off: the daily verdict and label will not run "
                   "unless something external triggers them.")
    return out
