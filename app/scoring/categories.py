"""Map a calendar event title to a category used by the gate.

Keyword order matters: the first category whose keywords match wins. This is the
crude-but-transparent Phase 1 mapping; Phase 3 replaces the impact judgement with
historically-learned weights.
"""
from __future__ import annotations

# Checked in this priority order.
CATEGORY_KEYWORDS: list[tuple[str, list[str]]] = [
    ("monetary_policy", [
        "fomc", "federal funds rate", "interest rate", "rate decision",
        "press conference", "fed chair", "monetary policy", "fomc minutes",
        "fed minutes", "powell", "fed monetary",
    ]),
    ("nfp", [
        "non-farm", "nonfarm", "nfp", "employment change", "unemployment rate",
        "average hourly earnings", "payrolls",
    ]),
    ("cpi", ["cpi", "consumer price index"]),
    ("ppi", ["ppi", "producer price"]),
    ("pce", ["pce", "personal consumption"]),
    ("gdp", ["gdp", "gross domestic product"]),
    ("retail_sales", ["retail sales"]),
    ("ism", ["ism manufacturing", "ism services", "ism non-manufacturing",
             "manufacturing pmi", "services pmi"]),
    ("jolts", ["jolts", "job openings"]),
    ("consumer_confidence", ["consumer confidence", "consumer sentiment",
                             "michigan consumer", "cb consumer"]),
]

# Friendly labels for the UI.
CATEGORY_LABELS = {
    "monetary_policy": "Monetary policy / FOMC",
    "nfp": "Jobs report (NFP)",
    "cpi": "CPI / inflation",
    "ppi": "PPI",
    "pce": "PCE",
    "gdp": "GDP",
    "retail_sales": "Retail sales",
    "ism": "ISM PMI",
    "jolts": "JOLTS",
    "consumer_confidence": "Consumer confidence",
}


def categorize(title: str) -> str | None:
    t = (title or "").lower()
    for category, keywords in CATEGORY_KEYWORDS:
        if any(kw in t for kw in keywords):
            return category
    return None
