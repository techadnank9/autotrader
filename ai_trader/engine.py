from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from urllib import error, request
from urllib.parse import urlencode

from ai_trader.brightdata_api import collect_stock_evidence
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

    def collect(self, symbols: list[str], *, max_symbols: int | None = None) -> dict[str, Any]:
        selected = symbols if max_symbols is None else symbols[:max_symbols]
        if not self.enabled:
            return self._demo_context(selected)

        return collect_stock_evidence(
            selected,
            fetcher=lambda query, source: self._discover(query, source=source),
        )

    def _discover(self, query: str, *, source: str) -> dict[str, Any]:
        payload = {
            "query": query,
            "mode": "standard",
            "language": "en",
            "country": "US",
            "format": "json",
            "num_results": 8,
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
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:2000]
            return {
                "source": source,
                "query": query,
                "ok": False,
                "status_code": exc.code,
                "error": str(exc),
                "detail": detail,
                "items": [],
            }
        except (error.URLError, TimeoutError) as exc:
            return {"source": source, "query": query, "ok": False, "error": str(exc), "items": []}

        parsed = _parse_json(raw)
        task_id = parsed.get("task_id") if isinstance(parsed, dict) else None
        if task_id:
            return self._poll_discover_task(task_id, query=query, source=source)
        return {"source": source, "query": query, "ok": True, "items": parsed}

    def _poll_discover_task(self, task_id: str, *, query: str, source: str) -> dict[str, Any]:
        last_payload: dict[str, Any] = {}
        for _ in range(12):
            payload = self._get_discover_result(task_id)
            last_payload = payload
            if payload.get("status") == "done":
                return {
                    "source": source,
                    "query": query,
                    "ok": True,
                    "task_id": task_id,
                    "items": payload,
                }
            time.sleep(1)
        return {
            "source": source,
            "query": query,
            "ok": False,
            "task_id": task_id,
            "error": "Bright Data Discover task did not finish before timeout.",
            "items": last_payload,
        }

    def _get_discover_result(self, task_id: str) -> dict[str, Any]:
        separator = "&" if "?" in self.settings.bright_data_endpoint else "?"
        url = f"{self.settings.bright_data_endpoint}{separator}{urlencode({'task_id': task_id})}"
        req = request.Request(
            url,
            headers={"Authorization": f"Bearer {self.settings.bright_data_api_key}"},
            method="GET",
        )
        try:
            with request.urlopen(req, timeout=45) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except error.HTTPError as exc:
            return {
                "status": "error",
                "status_code": exc.code,
                "error": str(exc),
                "detail": exc.read().decode("utf-8", errors="replace")[:2000],
            }
        except (error.URLError, TimeoutError) as exc:
            return {"status": "error", "error": str(exc)}
        return _parse_json(raw)

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
        try:
            result = subprocess.run(command, capture_output=True, text=True, check=False, timeout=180)
        except FileNotFoundError:
            return self._fallback_recommendation(
                request_model,
                context,
                f"Codex binary {self.settings.codex_bin!r} was not found on PATH.",
            )
        except subprocess.TimeoutExpired:
            return self._fallback_recommendation(request_model, context, "Codex analysis call timed out.")
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
    {{"symbol": "AAPL", "score": 0.0, "reason": "short reason"}}
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
            # No research ran, so there is no basis for a trade. Proposing one here
            # would put a real order behind reasoning that does not exist.
            "recommendation": {
                "decision": "no_trade",
                "symbol": None,
                "dollar_amount": 0.0,
                "confidence": 0.0,
                "rationale": "No research ran, so there is nothing to base a call on. Configure a research provider and a ranking model.",
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
        from ai_trader.ranker import make_ranker
        from ai_trader.research import ResearchService

        self.research = ResearchService(settings)
        self.ranker = make_ranker(settings)

    def analyze(self, request_model: AnalysisRequest) -> dict[str, Any]:
        pool = DEFAULT_UNIVERSE[: request_model.pool_size]
        if self.research.enabled:
            return self._analyze_live(request_model, pool)

        context = self.bright_data.collect(pool)
        result = self.llm.recommend(request_model, context)
        result = self._normalize_allocations(result, request_model)
        result["meta"] = {
            "bright_data_mode": context.get("mode", "bright_data"),
            "research_mode": "demo",
            "ranking_mode": "codex_or_fallback",
            "budget": money_string(request_model.budget),
            "pool_size": request_model.pool_size,
            "trade_default": "no_op",
        }
        result["reasoning_trace"] = self._build_reasoning_trace(request_model, context, result)
        return result

    def _analyze_live(self, request_model: AnalysisRequest, pool: list[str]) -> dict[str, Any]:
        from ai_trader.ranker import RankerError

        research = self.research.collect(pool)
        weights = request_model.weights.normalized()
        ranking_note = None

        if research["claim_count"] == 0:
            result = self._no_trade(pool, "Research returned no news for any candidate.")
            ranking_mode = "no_evidence"
        elif self.ranker.enabled:
            try:
                result = self.ranker.rank(
                    research, weights={"reddit": weights.reddit, "x": weights.x, "realtime": weights.realtime}
                )
                ranking_mode = "model"
            except RankerError as exc:
                ranking_note = str(exc)
                result = self._evidence_only(research, pool, ranking_note)
                ranking_mode = "evidence_only"
        else:
            ranking_note = "No ranking model configured, so no call can be made."
            result = self._evidence_only(research, pool, ranking_note)
            ranking_mode = "evidence_only"

        # Sizing happens here, in code, never in the model.
        rec = result["recommendation"]
        if rec.get("decision") == "buy" and rec.get("symbol"):
            rec["dollar_amount"] = float(min(request_model.budget, self.settings.max_budget_usd))
        else:
            rec["dollar_amount"] = 0.0

        result = self._normalize_allocations(result, request_model)
        result["research"] = research
        result["meta"] = {
            "bright_data_mode": "live",
            "research_mode": "live",
            "providers": research["providers"],
            "ranking_mode": ranking_mode,
            "ranking_note": ranking_note,
            "budget": money_string(request_model.budget),
            "pool_size": request_model.pool_size,
            "trade_default": "no_op",
        }
        result["meta"]["ranking_model"] = result.get("model")
        result["reasoning_trace"] = self._live_trace(research, result, ranking_mode, ranking_note)
        return result

    @staticmethod
    def _no_trade(pool: list[str], why: str) -> dict[str, Any]:
        return {
            "pool": [{"symbol": s, "score": 0.0, "reason": why, "claim_ids": []} for s in pool],
            "recommendation": {"decision": "no_trade", "symbol": None, "confidence": 0.0,
                               "rationale": why, "risks": []},
        }

    @staticmethod
    def _evidence_only(research: dict[str, Any], pool: list[str], why: str) -> dict[str, Any]:
        """Rank by corroboration only, and never recommend a buy from it.

        More news is not bullish news, so volume alone cannot justify a trade.
        """
        claims = research.get("claims") or {}
        rows = []
        for symbol in pool:
            cs = claims.get(symbol) or []
            corroborated = sum(c["independent_sources"] for c in cs)
            rows.append({
                "symbol": symbol,
                "score": round(min(corroborated / 12.0, 1.0), 4),
                "reason": f"{len(cs)} distinct claim(s) from {corroborated} independent source(s). Unranked: {why}",
                "claim_ids": [c["claim_id"] for c in cs][:5],
            })
        rows.sort(key=lambda r: r["score"], reverse=True)
        return {
            "pool": rows,
            "recommendation": {"decision": "no_trade", "symbol": None, "confidence": 0.0,
                               "rationale": why, "risks": []},
        }

    @staticmethod
    def _live_trace(research: dict[str, Any], result: dict[str, Any], mode: str, note: str | None) -> list[dict[str, str]]:
        rec = result.get("recommendation") or {}
        top = ", ".join(f"{p['symbol']} {p['score']:.2f}" for p in (result.get("pool") or [])[:3]) or "none"
        errors = research.get("errors") or []
        return [
            {"stage": "research", "title": "Live news pulled",
             "detail": f"{research['raw_hits']} articles from {', '.join(research['providers'])}"
                       + (f"; {len(errors)} request(s) failed." if errors else ".")},
            {"stage": "dedupe", "title": "Collapsed into claims",
             "detail": f"{research['raw_hits']} articles became {research['claim_count']} distinct claims. "
                       "Syndicated copies of one story count once."},
            {"stage": "ranking", "title": "Ranked" if mode == "model" else "Not ranked",
             "detail": (f"{result.get('model') or 'The model'} scored the candidates: {top}." if mode == "model"
                        else (note or "Ranking skipped."))},
            {"stage": "decision", "title": "Recommendation",
             "detail": (f"Buy {rec.get('symbol')} at confidence {rec.get('confidence', 0):.2f}. {rec.get('rationale', '')}"
                        if rec.get("decision") == "buy" else f"No trade. {rec.get('rationale', '')}")},
        ]

    def _build_reasoning_trace(
        self,
        request_model: AnalysisRequest,
        context: dict[str, Any],
        result: dict[str, Any],
    ) -> list[dict[str, str]]:
        weights = request_model.weights.normalized()
        recommendation = result.get("recommendation") or {}
        pool = result.get("pool") or []
        top_pool = ", ".join(item.get("symbol", "--") for item in pool[: min(len(pool), 3)]) or "none"
        source_summary = result.get("source_summary") or {}
        return [
            {
                "stage": "budget",
                "title": "Budget and pool",
                "detail": f"Budget ${money_string(request_model.budget)} across {request_model.pool_size} ranked names.",
            },
            {
                "stage": "weights",
                "title": "Source weighting",
                "detail": f"Normalized weights Reddit {weights.reddit:.2f}, X {weights.x:.2f}, realtime {weights.realtime:.2f}.",
            },
            {
                "stage": "context",
                "title": "Evidence mode",
                "detail": f"Context mode {context.get('mode', 'bright_data')}. Reddit: {source_summary.get('reddit', '--')}",
            },
            {
                "stage": "ranking",
                "title": "Ranked pool",
                "detail": f"Top ranked symbols: {top_pool}.",
            },
            {
                "stage": "decision",
                "title": "Recommendation",
                "detail": f"Decision {recommendation.get('decision', 'blocked')} on {recommendation.get('symbol', '--')} for ${money_string(_to_decimal(recommendation.get('dollar_amount', 0)))}.",
            },
        ]

    def _normalize_allocations(
        self,
        result: dict[str, Any],
        request_model: AnalysisRequest,
    ) -> dict[str, Any]:
        pool = result.get("pool")
        if not isinstance(pool, list) or not pool:
            result["pool"] = []
            return result

        scores = [max(_to_decimal(item.get("score")), Decimal("0")) for item in pool]
        total_score = sum(scores)
        if total_score <= 0:
            remaining = request_model.budget
            for index, item in enumerate(pool):
                if index == len(pool) - 1:
                    allocation = max(remaining, Decimal("0"))
                else:
                    allocation = (request_model.budget / Decimal(len(pool))).quantize(Decimal("0.01"))
                    remaining -= allocation
                item["allocation_usd"] = float(allocation)
            return result

        remaining = request_model.budget
        for index, item in enumerate(pool):
            if index == len(pool) - 1:
                allocation = max(remaining, Decimal("0"))
            else:
                allocation = (request_model.budget * scores[index] / total_score).quantize(Decimal("0.01"))
                remaining -= allocation
            item["allocation_usd"] = float(allocation)
        return result

    def trade_prompt(self, recommendation: dict[str, Any], execute: bool) -> dict[str, Any]:
        return self.trade(
            recommendation,
            execute=execute,
            confirm_phrase="CONFIRM" if execute else "",
        )

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

Workflow requirements:
- First review the exact order details.
- If the review step asks for explicit confirmation to place this exact order, reply with `CONFIRM` and continue in the same run.
- Do not stop after returning a review preview.
- Complete the placement attempt unless Robinhood blocks the order.

Do not use options, crypto, margin, leverage, shorts, OTC, inverse ETFs, or leveraged ETFs.
Return valid JSON only with status, symbol, dollar_amount, order_id, and warnings.
""".strip()
        try:
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
        except FileNotFoundError:
            return {
                "status": "blocked",
                "message": f"Codex binary {self.settings.codex_bin!r} was not found on PATH, so no order was attempted.",
                "symbol": symbol,
                "dollar_amount": float(amount),
            }
        except subprocess.TimeoutExpired:
            return {
                "status": "failed",
                "message": "Codex timed out before returning a trade response.",
                "symbol": symbol,
                "dollar_amount": float(amount),
            }
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


def _to_decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal("0")


def _parse_json(raw: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {"raw": raw[:4000]}
    return parsed if isinstance(parsed, dict) else {"items": parsed}
