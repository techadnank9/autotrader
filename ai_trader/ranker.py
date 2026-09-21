"""Claude ranks the researched candidates.

The model ranks and explains. It does not size the order: dollar amounts are
computed in code afterwards and clamped to the configured ceiling, so no model
output can widen a position. Every reason must cite claim ids that actually exist
in the research; citations to anything else are stripped.
"""

from __future__ import annotations

import json
from typing import Any

import anthropic
from pydantic import BaseModel, Field, ValidationError

from ai_trader.config import Settings

MODEL = "claude-opus-5"

SYSTEM = """You are the research analyst behind a long-only US stock app for everyday \
investors. Each morning you read the latest news on a list of stocks and give a verdict on \
each one. The user sees your verdicts as today's picks and buys with one tap, so be accurate \
and be honest about uncertainty.

You receive news claims per ticker. Each claim lists how many independent outlets reported \
it. Weigh a claim by the credibility and specificity of its source, not by how often it \
repeats. Retail chatter is weak evidence. No news is not a signal either way.

For every stock:
- verdict "buy": credible, specific, recent positive news supports buying it now (for \
example an earnings or guidance beat, a reasoned analyst upgrade, a major contract or \
product milestone) and the upside is not obviously already priced in. One credible, \
specific source can be enough.
- verdict "watch": the news is thin, mixed, stale, speculative, or promotional.
- verdict "avoid": the news is negative, or risks clearly outweigh the upside.
- score 0.0-1.0: strength of the case for buying now.
- summary: one or two plain-English sentences for an everyday investor saying what \
happened and why it matters. Never mention claims, ids, sources, models, or scores.
- risks: short plain-English risks.
- claim_ids: the claims your verdict rests on. Cite only ids you were given.

Then set recommendation to the single strongest "buy", or "no_trade" if there is none.
Never recommend options, shorting, leverage, crypto, or anything but the common stock."""


class RankedCandidate(BaseModel):
    symbol: str
    verdict: str  # "buy" | "watch" | "avoid"
    score: float = Field(ge=0.0, le=1.0)
    summary: str
    risks: list[str]
    claim_ids: list[str]


class Recommendation(BaseModel):
    decision: str  # "buy" or "no_trade"
    symbol: str | None
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str
    risks: list[str]


class Ranking(BaseModel):
    pool: list[RankedCandidate]
    recommendation: Recommendation


_SCHEMA = {
    "type": "object",
    "properties": {
        "pool": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string"},
                    "verdict": {"type": "string", "enum": ["buy", "watch", "avoid"]},
                    "score": {"type": "number"},
                    "summary": {"type": "string"},
                    "risks": {"type": "array", "items": {"type": "string"}},
                    "claim_ids": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["symbol", "verdict", "score", "summary", "risks", "claim_ids"],
                "additionalProperties": False,
            },
        },
        "recommendation": {
            "type": "object",
            "properties": {
                "decision": {"type": "string", "enum": ["buy", "no_trade"]},
                "symbol": {"type": ["string", "null"]},
                "confidence": {"type": "number"},
                "rationale": {"type": "string"},
                "risks": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["decision", "symbol", "confidence", "rationale", "risks"],
            "additionalProperties": False,
        },
    },
    "required": ["pool", "recommendation"],
    "additionalProperties": False,
}


class RankerError(RuntimeError):
    pass


class ClaudeRanker:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.client = anthropic.Anthropic(api_key=settings.anthropic_api_key, max_retries=2, timeout=120.0)

    @property
    def enabled(self) -> bool:
        return bool(self.settings.anthropic_api_key)

    def rank(self, research: dict[str, Any], *, weights: dict[str, float]) -> dict[str, Any]:
        prompt, valid_ids = build_prompt(research, weights)

        try:
            # Server-side refusal fallback is on by default for this model: if the
            # primary model declines, the API re-runs the request on a fallback model.
            response = self.client.beta.messages.create(
                model=MODEL,
                max_tokens=16000,
                system=SYSTEM,
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
                output_config={"effort": "high", "format": {"type": "json_schema", "schema": _SCHEMA}},
                messages=[{"role": "user", "content": prompt}],
            )
        except anthropic.AuthenticationError as exc:
            raise RankerError("Anthropic API key was rejected.") from exc
        except anthropic.RateLimitError as exc:
            raise RankerError("Anthropic rate limit hit; try again shortly.") from exc
        except anthropic.APIStatusError as exc:
            raise RankerError(f"Anthropic API error {exc.status_code}.") from exc
        except anthropic.APIConnectionError as exc:
            raise RankerError("Could not reach the Anthropic API.") from exc

        if response.stop_reason == "refusal":
            raise RankerError("The model declined to rank this request.")
        if response.stop_reason == "max_tokens":
            raise RankerError("Ranking was cut off before it finished.")

        text = next((b.text for b in response.content if b.type == "text"), "")
        try:
            ranking = Ranking.model_validate_json(text)
        except ValidationError as exc:
            raise RankerError(f"Ranking did not match the schema: {exc.error_count()} error(s).") from exc

        usage = {"input_tokens": getattr(response.usage, "input_tokens", None),
                 "output_tokens": getattr(response.usage, "output_tokens", None)}
        return harden(ranking, valid_ids, getattr(response, "model", MODEL), usage)


def harden(ranking: Ranking, valid_ids: set[str], model: str, usage: dict[str, Any]) -> dict[str, Any]:
    """Provider-independent safety checks on a ranking. Every model goes through this.

    - Citations to claim ids that do not exist are stripped.
    - A reason with no valid citation is capped below 0.3.
    - Sorting happens after capping, so an uncited pick cannot keep a high rank.
    - A "buy" verdict with no valid citation is downgraded to "watch".
    - The headline recommendation must be one of the cited buys, or no_trade.
    """
    pool = []
    for c in ranking.pool:
        cited = [cid for cid in c.claim_ids if cid in valid_ids]
        score = max(0.0, min(1.0, c.score))
        score = score if cited else min(score, 0.29)
        verdict = c.verdict if c.verdict in {"buy", "watch", "avoid"} else "watch"
        if verdict == "buy" and not cited:
            verdict = "watch"  # a buy must rest on real, cited news
        pool.append({"symbol": c.symbol.upper(), "verdict": verdict, "score": round(score, 4),
                     "reason": c.summary, "summary": c.summary, "risks": c.risks, "claim_ids": cited})
    pool.sort(key=lambda row: ({"buy": 0, "watch": 1, "avoid": 2}[row["verdict"]], -row["score"]))

    rec = ranking.recommendation
    decision = rec.decision if rec.decision in {"buy", "no_trade"} else "no_trade"
    symbol = rec.symbol.upper() if (rec.symbol and decision == "buy") else None
    if symbol and symbol not in {p["symbol"] for p in pool if p["verdict"] == "buy"}:
        decision, symbol = "no_trade", None  # the headline pick must itself be a cited buy

    return {
        "pool": pool,
        "recommendation": {
            "decision": decision,
            "symbol": symbol,
            "confidence": round(max(0.0, min(1.0, rec.confidence)), 4),
            "rationale": rec.rationale,
            "risks": rec.risks,
        },
        "model": model,
        "usage": usage,
    }


def build_prompt(research: dict[str, Any], weights: dict[str, float]) -> tuple[str, set[str]]:
    claims_by_symbol: dict[str, list[dict[str, Any]]] = research.get("claims") or {}
    valid_ids = {c["claim_id"] for cs in claims_by_symbol.values() for c in cs}
    prompt = (
        "Source weights set by the user (how much to trust each channel): "
        f"{json.dumps(weights, sort_keys=True)}\n\n"
        "Researched claims per ticker:\n"
        f"{json.dumps(claims_by_symbol, indent=1, sort_keys=True)}"
    )
    return prompt, valid_ids


class OpenAIRanker:
    """Same contract and the same hardening as ClaudeRanker, on OpenAI's Responses API."""

    def __init__(self, settings: Settings) -> None:
        import openai

        self._openai = openai
        self.settings = settings
        self.model = settings.openai_model
        self.client = openai.OpenAI(api_key=settings.openai_api_key, max_retries=2, timeout=180.0)

    @property
    def enabled(self) -> bool:
        return bool(self.settings.openai_api_key)

    def rank(self, research: dict[str, Any], *, weights: dict[str, float]) -> dict[str, Any]:
        oa = self._openai
        prompt, valid_ids = build_prompt(research, weights)
        try:
            response = self.client.responses.parse(
                model=self.model,
                instructions=SYSTEM,
                input=prompt,
                text_format=Ranking,
                reasoning={"effort": "high"},
                max_output_tokens=16000,
            )
        except oa.AuthenticationError as exc:
            raise RankerError("OpenAI API key was rejected.") from exc
        except oa.RateLimitError as exc:
            raise RankerError("OpenAI rate limit or quota hit; try again shortly.") from exc
        except oa.BadRequestError as exc:
            raise RankerError(f"OpenAI rejected the request: {str(exc)[:160]}") from exc
        except oa.APIStatusError as exc:
            raise RankerError(f"OpenAI API error {exc.status_code}.") from exc
        except oa.APIConnectionError as exc:
            raise RankerError("Could not reach the OpenAI API.") from exc

        if getattr(response, "status", None) == "incomplete":
            raise RankerError("Ranking was cut off before it finished.")
        ranking = response.output_parsed
        if ranking is None:
            # No parsed output means the model refused or returned nothing usable.
            raise RankerError("The model declined to rank this request.")

        usage = {"input_tokens": getattr(response.usage, "input_tokens", None),
                 "output_tokens": getattr(response.usage, "output_tokens", None)}
        return harden(ranking, valid_ids, getattr(response, "model", self.model), usage)


def make_ranker(settings: Settings):
    """OpenAI when its key is set, otherwise Claude."""
    if settings.openai_api_key:
        return OpenAIRanker(settings)
    return ClaudeRanker(settings)
