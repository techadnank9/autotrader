from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from urllib import error, request

from ai_trader.config import Settings
from ai_trader.utils import money_string

DEFAULT_UNIVERSE = [
    "AAPL",
    "MSFT",
    "NVDA",
    "GOOGL",
    "AMZN",
    "META",
    "AMD",
    "TSLA",
    "AVGO",
    "COST",
]


@dataclass(frozen=True)
class SourceWeights:
    reddit: float
    x: float
    realtime: float

    def normalized(self) -> "SourceWeights":
        total = max(self.reddit + self.x + self.realtime, 0.001)
        return SourceWeights(
            reddit=self.reddit / total,
            x=self.x / total,
            realtime=self.realtime / total,
        )


@dataclass(frozen=True)
class AnalysisRequest:
    budget: Decimal
    pool_size: int
    weights: SourceWeights


class BrightDataClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @property
    def enabled(self) -> bool:
        return bool(self.settings.bright_data_api_key)

    def collect(self, symbols: list[str]) -> dict[str, Any]:
        if not self.enabled:
            return self._demo_context(symbols)

        return {
            "mode": "bright_data",
            "reddit": self._discover(
                f"Reddit stock discussion latest news {' '.join(symbols[:4])}",
                source="reddit",
            ),
            "x": self._discover(
                f"X Twitter stock market latest news {' '.join(symbols[:4])}",
                source="x",
            ),
            "realtime": self._discover(
                f"{' '.join(symbols[:4])} latest earnings news realtime stock market",
                source="realtime",
            ),
        }

    def _discover(self, query: str, *, source: str) -> dict[str, Any]:
        payload = {
            "query": query,
            "mode": "standard",
            "language": "en",
            "country": "US",
            "format": "json",
        }
        req = request.Request(
            self.settings.bright_data_endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.settings.bright_data_api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=45) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except (error.HTTPError, error.URLError, TimeoutError) as exc:
            return {"source": source, "query": query, "ok": False, "error": str(exc), "items": []}

        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = {"raw": raw[:4000]}
        return {"source": source, "query": query, "ok": True, "items": parsed}

    def _demo_context(self, symbols: list[str]) -> dict[str, Any]:
        return {
            "mode": "demo",
            "reddit": [
                {"symbol": symbols[0], "title": "Retail discussion clustered around large-cap earnings quality."},
                {"symbol": symbols[min(1, len(symbols) - 1)], "title": "Risk thread mentions valuation sensitivity."},
            ],
            "x": [
                {"symbol": symbols[0], "title": "Momentum chatter is positive but headline-driven."},
                {"symbol": symbols[min(2, len(symbols) - 1)], "title": "Traders watching semiconductor breadth."},
            ],
            "realtime": [
                {"symbol": symbol, "title": "Realtime quote should be verified through Robinhood before order review."}
                for symbol in symbols[:4]
            ],
        }


class CodexLLM:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def recommend(self, request_model: AnalysisRequest, context: dict[str, Any]) -> dict[str, Any]:
        prompt = self._prompt(request_model, context)
        command = [
            self.settings.codex_bin,
            "exec",
            "--skip-git-repo-check",
            "--json",
            prompt,
        ]
        result = subprocess.run(command, capture_output=True, text=True, check=False, timeout=180)
        text = self._extract_final_text(result.stdout)
        if result.returncode == 0 and text:
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return {"raw": text, "parse_error": True}
        return self._fallback_recommendation(request_model, context, result.stderr)

    def _prompt(self, request_model: AnalysisRequest, context: dict[str, Any]) -> str:
        weights = request_model.weights.normalized()
        return f"""
You are a cautious equities research agent.

Budget: ${money_string(request_model.budget)}
Pool size: {request_model.pool_size}
Source weights:
- reddit: {weights.reddit:.2f}
- x: {weights.x:.2f}
- realtime: {weights.realtime:.2f}

Use this Bright Data context and rank a pool of stocks. Pick exactly one recommendation or no_trade.

Context JSON:
{json.dumps(context, indent=2)[:12000]}

Return valid JSON only:
{{
  "pool": [
    {{"symbol": "AAPL", "score": 0.0, "reason": "short reason", "allocation_usd": 0.0}}
  ],
  "recommendation": {{
    "decision": "buy" | "no_trade",
    "symbol": "string or null",
    "dollar_amount": number,
    "confidence": 0.0,
    "rationale": "short paragraph",
    "risks": ["risk", "risk"]
  }},
  "source_summary": {{
    "reddit": "short finding",
    "x": "short finding",
    "realtime": "short finding"
  }}
}}
""".strip()

    def _fallback_recommendation(
        self,
        request_model: AnalysisRequest,
        context: dict[str, Any],
        stderr: str,
    ) -> dict[str, Any]:
        symbols = DEFAULT_UNIVERSE[: request_model.pool_size]
        budget = float(request_model.budget)
        pool = [
            {
                "symbol": symbol,
                "score": round(0.72 - index * 0.04, 2),
                "reason": "Fallback ranking until Codex returns a parseable recommendation.",
                "allocation_usd": round(budget / max(len(symbols), 1), 2),
            }
            for index, symbol in enumerate(symbols)
        ]
        return {
            "pool": pool,
            "recommendation": {
                "decision": "buy" if pool else "no_trade",
                "symbol": pool[0]["symbol"] if pool else None,
                "dollar_amount": float(min(request_model.budget, Decimal("5"))),
                "confidence": 0.58,
                "rationale": "Fallback recommendation generated because the Codex analysis call did not complete cleanly.",
                "risks": ["Validate tradability and quote context before any live order.", stderr[:180] if stderr else "No stderr."],
            },
            "source_summary": {
                "reddit": f"{context.get('mode', 'unknown')} context loaded.",
                "x": "Social signal is weighted but not treated as authoritative.",
                "realtime": "Robinhood review remains required before execution.",
            },
        }

    def _extract_final_text(self, stdout: str) -> str | None:
        last_text: str | None = None
        for line in stdout.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            item = event.get("item", {})
            if event.get("type") == "item.completed" and item.get("type") == "agent_message":
                last_text = item.get("text")
        return last_text


class RecommendationEngine:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.bright_data = BrightDataClient(settings)
        self.llm = CodexLLM(settings)

    def analyze(self, request_model: AnalysisRequest) -> dict[str, Any]:
        pool = DEFAULT_UNIVERSE[: request_model.pool_size]
        context = self.bright_data.collect(pool)
        result = self.llm.recommend(request_model, context)
        result["meta"] = {
            "bright_data_mode": context.get("mode", "bright_data"),
            "budget": money_string(request_model.budget),
            "pool_size": request_model.pool_size,
            "trade_default": "no_op",
        }
        return result

    def trade_prompt(self, recommendation: dict[str, Any], execute: bool) -> dict[str, Any]:
        return self.trade(recommendation, execute=execute, confirm_phrase="")

    def trade(
        self,
        recommendation: dict[str, Any],
        *,
        execute: bool,
        confirm_phrase: str,
    ) -> dict[str, Any]:
        symbol = recommendation.get("symbol")
        amount = Decimal(str(recommendation.get("dollar_amount") or "0"))
        if not execute:
            return {
                "status": "no_op",
                "message": "Trade execution skipped. Default is no-op.",
                "symbol": symbol,
                "dollar_amount": float(amount),
            }
        if confirm_phrase != "CONFIRM":
            return {
                "status": "blocked",
                "message": "Real execution requires CONFIRM.",
                "symbol": symbol,
                "dollar_amount": float(amount),
            }
        if not symbol or amount <= 0 or amount > self.settings.max_budget_usd:
            return {
                "status": "blocked",
                "message": f"Invalid symbol or amount above max budget {self.settings.max_budget_usd}.",
                "symbol": symbol,
                "dollar_amount": float(amount),
            }

        prompt = f"""
Use the `robinhood-trading` MCP.

Review and place exactly one long-only U.S. equity buy order:
- Symbol: {symbol}
- Dollar amount: ${money_string(amount)}

Do not use options, crypto, margin, leverage, shorts, OTC, inverse ETFs, or leveraged ETFs.
Return valid JSON only with status, symbol, dollar_amount, order_id, and warnings.
""".strip()
        result = subprocess.run(
            [
                self.settings.codex_bin,
                "exec",
                "--skip-git-repo-check",
                "--json",
                prompt,
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=240,
        )
        text = self.llm._extract_final_text(result.stdout)
        if text:
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return {"status": "submitted_unparsed", "raw": text}
        return {
            "status": "failed",
            "message": "Codex did not return a final trade response.",
            "stderr": result.stderr[-1200:],
            "returncode": result.returncode,
        }
