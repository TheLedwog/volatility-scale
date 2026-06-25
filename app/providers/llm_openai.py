"""OpenAI/GPT headline-scoring provider (interface ready; used from Phase 4).

It reads headlines and returns a structured market-impact judgement. Kept behind
the LLMProvider interface so the model/provider can be swapped later.
"""
from __future__ import annotations

import json

from ..config import get_config
from .base import LLMProvider

_SYSTEM = (
    "You are a markets analyst. Given today's headlines, judge their impact on the "
    "US equity index NY session (S&P 500 / NASDAQ). Reply ONLY with JSON: "
    '{"relevance": 0..1, "direction": "risk_on|risk_off|uncertain", '
    '"expected_impact": 0..1, "rationale": "one sentence"}.'
)

_NEUTRAL = {"relevance": 0.0, "direction": "uncertain",
            "expected_impact": 0.0, "rationale": "no LLM key configured"}


class OpenAILLMProvider(LLMProvider):
    def score_headlines(self, headlines: list[str]) -> dict:
        cfg = get_config()["providers"]
        api_key = cfg.get("openai_api_key", "")
        if not api_key or not headlines:
            return dict(_NEUTRAL)
        try:
            from openai import OpenAI

            client = OpenAI(api_key=api_key)
            resp = client.chat.completions.create(
                model=cfg.get("openai_model", "gpt-4o-mini"),
                messages=[
                    {"role": "system", "content": _SYSTEM},
                    {"role": "user", "content": "\n".join(f"- {h}" for h in headlines)},
                ],
                temperature=0,
                response_format={"type": "json_object"},
            )
            return json.loads(resp.choices[0].message.content)
        except Exception as exc:  # noqa: BLE001 - degrade gracefully
            return {**_NEUTRAL, "rationale": f"llm error: {exc}"}
