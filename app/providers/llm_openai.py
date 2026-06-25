"""OpenAI/GPT news scorer — reads today's headlines and judges chop risk.

Grounded in the user's strategy: they trade the NY session of the NASDAQ/S&P 500,
want big *directional* moves, and lose on erratic, headline-driven whipsaw (chop).
Behind the LLMProvider interface so the model/provider can be swapped later.
"""
from __future__ import annotations

import json

from .base import LLMProvider

_SYSTEM = (
    "You are a markets analyst for an intraday trader of the NEW YORK session of the "
    "NASDAQ-100 and S&P 500. The trader needs big, clean DIRECTIONAL moves and loses "
    "money on erratic, headline-driven, whipsaw 'chop' days. Given today's news "
    "headlines, judge how today's NY session is likely to behave. Reply with ONLY a "
    "JSON object: {"
    '"relevance": 0..1 (how market-relevant the news is), '
    '"expected_impact": 0..1 (how much it could move US equities), '
    '"direction": "risk_on" | "risk_off" | "uncertain", '
    '"chop_risk": 0..1 (probability the session is erratic/whippy and hard to trade; '
    "raise it when news is high-impact but conflicting/uncertain, lower it when there "
    'is a single clear directional catalyst), '
    '"rationale": "one short sentence"}.'
)


def _clamp01(v):
    try:
        return max(0.0, min(1.0, float(v)))
    except (TypeError, ValueError):
        return None


class OpenAILLMProvider(LLMProvider):
    def score_news(self, headlines: list[str], cfg: dict) -> dict:
        from ..config import openai_api_key

        prov = cfg["providers"]
        api_key = openai_api_key(cfg)
        if not api_key or not headlines:
            return {"scored": False}
        try:
            from openai import OpenAI

            client = OpenAI(api_key=api_key)
            resp = client.chat.completions.create(
                model=prov.get("openai_model", "gpt-4o-mini"),
                messages=[
                    {"role": "system", "content": _SYSTEM},
                    {"role": "user", "content": "\n".join(f"- {h}" for h in headlines)},
                ],
                temperature=0,
                response_format={"type": "json_object"},
            )
            data = json.loads(resp.choices[0].message.content)
            return {
                "scored": True,
                "relevance": _clamp01(data.get("relevance")),
                "expected_impact": _clamp01(data.get("expected_impact")),
                "direction": data.get("direction", "uncertain"),
                "chop_risk": _clamp01(data.get("chop_risk")),
                "rationale": str(data.get("rationale", ""))[:300],
            }
        except Exception as exc:  # noqa: BLE001 - degrade gracefully
            return {"scored": False, "error": f"llm error: {exc}"}
