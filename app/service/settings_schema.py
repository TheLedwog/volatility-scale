"""Declarative description of every setting the admin API lets you change.

This is the single source of truth for BOTH halves of the settings surface: the
`schema` array the frontend renders its controls from, and the validator that
accepts or rejects an incoming patch. Keeping them in one table is the point - a
bound that exists in the UI but not the validator is a lie the moment anyone calls
the API directly, and a bound that exists only in the validator is a form that
rejects on submit for no visible reason.

Adding a setting to the frontend is therefore one entry here and no frontend change:
the panel builds its controls from `type`/`choices`/`min`/`max` and labels them from
`label`/`help`.

The help text is lifted from the comments in app/config.py, so the frontend can
explain a field as well as the Jinja page does.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field as dc_field

# Enumerations shared with the Jinja settings page (imported by app/main.py, so the
# two surfaces can't drift apart on what a valid choice is).
OPENAI_MODELS = ("gpt-5.6-luna", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.5",
                 "gpt-4o-mini", "gpt-4o")
SCORING_MODES = ("auto", "rules", "model")

# Config paths holding credentials. These are never returned by a GET and never
# accepted by a settings PATCH - they have their own write-only endpoints, so there
# is no way to leak one by reading and no way to blank one by round-tripping a form.
SECRET_PATHS = (
    "providers.openai_api_key",
    "providers.webz_api_key",
    "providers.news_api_key",
)


@dataclass(frozen=True)
class Field:
    """One editable config leaf.

    `path` is the dotted route into the config dict. `type` drives both the control
    the frontend renders and the coercion the validator applies:

        int | float   numeric, with optional min/max/step
        bool          checkbox
        str           single-line text
        text          multi-line textarea (news query, etc.)
        time          "HH:MM", validated as a real clock time
        enum          one of `choices`
        json          a whole structural section, edited as raw JSON
    """
    path: str
    type: str
    label: str
    section: str
    help: str = ""
    min: float | None = None
    max: float | None = None
    step: float | None = None
    choices: tuple[str, ...] | None = None
    # Set on fields whose value only takes effect somewhere non-obvious, so the panel
    # can warn before the save rather than after.
    note: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["choices"] = list(self.choices) if self.choices else None
        return d


@dataclass(frozen=True)
class Section:
    """A group of fields = one card on the settings page."""
    name: str
    title: str
    description: str = ""
    advanced: bool = False
    fields: list = dc_field(default_factory=list)


# ── The registry ────────────────────────────────────────────────────────────────
# Order here is the order the frontend should render.

SECTIONS: tuple[Section, ...] = (
    Section(
        "scoring", "Scoring & news",
        "Which engine produces the score, and whether the news factor runs at all.",
    ),
    Section(
        "thresholds", "Thresholds",
        "Where the verdict lines fall, and how a realized session gets graded.",
    ),
    Section(
        "schedule", "Schedule",
        "When the daily jobs run. Times are in the session timezone (America/New_York).",
    ),
    Section(
        "weights", "Factor weights",
        "Relative importance of each rule-based factor. Normalised automatically, so "
        "they need not add up to any particular number.",
    ),
    Section(
        "advanced", "Advanced",
        "Structural config, edited as raw JSON: the instruments, the session hours, "
        "the event categories that trip the gate, and the model's training gates.",
        advanced=True,
    ),
)

FIELDS: tuple[Field, ...] = (
    # ── Scoring & news ──────────────────────────────────────────────────────────
    Field("scoring.mode", "enum", "Scoring engine", "scoring",
          help="'auto' uses the trained model only if it beats the rules on its "
               "hold-out, otherwise the rule-based factors. 'rules'/'model' force one.",
          choices=SCORING_MODES),
    Field("news.enabled", "bool", "Enable news / GPT scoring", "scoring",
          help="Fetch headlines. The GPT read of them additionally needs an OpenAI key."),
    Field("providers.openai_model", "enum", "OpenAI model", "scoring",
          help="Model used for the news read.", choices=OPENAI_MODELS),
    Field("news.max_headlines", "int", "Max headlines", "scoring",
          help="How many headlines are passed to the GPT read.", min=1, max=100),
    Field("news.query", "text", "News query (GDELT)", "scoring",
          help="GDELT GKG theme filters plus source language/country. Themes rather "
               "than loose keywords: keyword search over article text pulls in "
               "newest-anything junk."),

    # ── Thresholds ──────────────────────────────────────────────────────────────
    Field("thresholds.good", "int", "Good to trade ≥", "thresholds",
          help="Direction-quality score (0-100) at or above which the day reads as "
               "tradeable. Must sit above the caution line.", min=0, max=100),
    Field("thresholds.caution", "int", "Choppy / avoid <", "thresholds",
          help="Below this score the day reads as choppy.", min=0, max=100),
    Field("thresholds.dead_day_range_pct", "float", "Dead-day ATR%", "thresholds",
          help="ATR% below this flags the day as low-opportunity.", min=0, max=100,
          step=0.01),
    Field("thresholds.label_directional_er", "float", "Label: directional ER", "thresholds",
          help="Realized 5-min efficiency ratio at or above which a session is graded "
               "DIRECTIONAL. Set as a percentile of the measured distribution.",
          min=0, max=1, step=0.01,
          note="Changing this re-bases every stored label; the track record needs a "
               "re-grade to stay comparable."),
    Field("thresholds.label_choppy_er", "float", "Label: choppy ER", "thresholds",
          help="Realized efficiency ratio below which a session is graded CHOPPY. "
               "Must sit below the directional cutoff.",
          min=0, max=1, step=0.01,
          note="Changing this re-bases every stored label; the track record needs a "
               "re-grade to stay comparable."),

    # ── Schedule ────────────────────────────────────────────────────────────────
    Field("schedule.enabled", "bool", "Run scheduled jobs", "schedule",
          help="Turn the in-process scheduler off if you drive the jobs from cron."),
    Field("schedule.predict_time", "time", "Pre-open predict", "schedule",
          help="When the frozen verdict is cut. 09:30 is the open, by which point the "
               "full pre-market reaction to an 08:30 data drop is in the bars."),
    Field("schedule.label_time", "time", "Post-close label", "schedule",
          help="When the realized session is graded."),

    # ── Advanced (raw JSON sections) ────────────────────────────────────────────
    Field("session", "json", "session", "advanced",
          help="Timezone and session hours."),
    Field("tickers", "json", "tickers", "advanced",
          help="The instruments the score is computed from."),
    Field("gate", "json", "gate", "advanced",
          help="Which event categories VETO the day and which only WARN, the minimum "
               "impact that trips the gate, and the pre-open buffer."),
    Field("ml", "json", "ml", "advanced",
          help="Training windows, the hold-out fraction, and the metrics the model "
               "must clear before 'auto' mode will use it over the rules."),
)

_BY_PATH = {f.path: f for f in FIELDS}


def weight_fields(cfg: dict) -> list[Field]:
    """Factor weights, expanded from the live config.

    Generated rather than hard-coded so a new factor added to DEFAULTS["weights"]
    appears on the frontend with no change here and none in the frontend either.
    """
    return [
        Field(f"weights.{name}", "float", name.replace("_", " "), "weights",
              help="Relative weight of this factor before normalisation.",
              min=0, step=0.01)
        for name in (cfg.get("weights") or {})
    ]


def all_fields(cfg: dict) -> list[Field]:
    """Every field, static plus the config-derived weights, in render order."""
    order = {s.name: i for i, s in enumerate(SECTIONS)}
    fields = list(FIELDS) + weight_fields(cfg)
    return sorted(fields, key=lambda f: order.get(f.section, 99))


def field_map(cfg: dict) -> dict[str, Field]:
    """Dotted path -> Field, including the expanded weights."""
    return {f.path: f for f in all_fields(cfg)}


def sections_payload() -> list[dict]:
    return [{"name": s.name, "title": s.title, "description": s.description,
             "advanced": s.advanced} for s in SECTIONS]


def is_secret(path: str) -> bool:
    return path in SECRET_PATHS
