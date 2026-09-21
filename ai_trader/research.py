"""Live market research: fetch news concurrently, then collapse it into claims.

Two providers, each used for what it does best:

- Tavily: news search with a native freshness filter (`topic=news`, `time_range`).
- Parallel: objective-driven web search with a publish-date floor.

Every hit becomes evidence; evidence that says the same thing is clustered into a
single claim. A story syndicated across forty outlets is one claim with forty
sources, not forty signals. The ranker sees claims, never raw hits.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any
from urllib.parse import urlparse

import httpx

from ai_trader.config import Settings

TAVILY_URL = "https://api.tavily.com/search"
PARALLEL_URL = "https://api.parallel.ai/v1/search"
MAX_CONCURRENCY = 8
CLAIM_SIMILARITY = 0.5

_STOP = set(
    "a an the and or of to in on for with at by from as is are was were be been it its "
    "this that these those stock stocks share shares inc corp co ltd says said after "
    "amid over into new today report reports".split()
)


@dataclass
class Evidence:
    symbol: str
    provider: str
    title: str
    url: str
    domain: str
    published: str | None
    snippet: str

    def tokens(self) -> set[str]:
        words = re.findall(r"[a-z0-9]+", self.title.lower())
        return {w for w in words if w not in _STOP and len(w) > 2}


@dataclass
class Claim:
    claim_id: str
    symbol: str
    headline: str
    snippet: str
    evidence: list[Evidence] = field(default_factory=list)

    @property
    def domains(self) -> list[str]:
        return sorted({e.domain for e in self.evidence if e.domain})

    @property
    def independent_sources(self) -> int:
        return len(self.domains)

    @property
    def latest(self) -> str | None:
        dates = [e.published for e in self.evidence if e.published]
        return max(dates) if dates else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "claim_id": self.claim_id,
            "symbol": self.symbol,
            "headline": self.headline,
            "snippet": self.snippet,
            "independent_sources": self.independent_sources,
            "domains": self.domains,
            "latest": self.latest,
            "urls": [e.url for e in self.evidence][:6],
        }


# Pages that are not news: quote hubs, headline indexes, options chains, 13F
# filler, and promotional projections. Their generic titles look alike across
# sites, so if they reach clustering they masquerade as independently
# corroborated claims, which is exactly the signal the ranker trusts most.
_NOT_NEWS_TITLE = re.compile(
    r"("
    r"\(\s*\$?[a-z.]{1,6}\s*\)\s*stock price"                       # "Amazon.com (AMZN) Stock Price ..."
    r"|stock price\s*(&|,|and)\s*(news|overview|quote|history|chart)"  # "Stock Price, News, Quote & History"
    r"|\bstock quotes?\b|quotes? (&|and) news|\|\s*quotes?\b"          # quote hubs
    r"|latest stock news|news (&|and) headlines|\bnews today\b"         # headline indexes
    r"|options chain"
    r"|\bshares? (in|of) .{1,60}\b(bought|sold|acquired) by\b"          # 13F filler
    r"|\bcould grow to\b"                                              # promotional projections
    r")",
    re.IGNORECASE,
)
_NOT_NEWS_URL = re.compile(
    r"(/quote/|/quotes/|/symbol/|/market-activity/stocks/[a-z.]+/?$|/stocks/[a-z.]+/?$|"
    r"/stock/[a-z.]+/?$|/options|/news/?$|/chart)",
    re.IGNORECASE,
)


def is_news(ev: "Evidence") -> bool:
    return not (_NOT_NEWS_TITLE.search(ev.title) or _NOT_NEWS_URL.search(urlparse(ev.url).path))


def _domain(url: str) -> str:
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


class ResearchService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @property
    def providers(self) -> list[str]:
        live = []
        if self.settings.tavily_api_key:
            live.append("tavily")
        if self.settings.parallel_api_key:
            live.append("parallel")
        return live

    @property
    def enabled(self) -> bool:
        return bool(self.providers)

    # -- fetching ------------------------------------------------------------

    async def _tavily(self, client: httpx.AsyncClient, symbol: str) -> list[Evidence]:
        resp = await client.post(
            TAVILY_URL,
            headers={"Authorization": f"Bearer {self.settings.tavily_api_key}"},
            json={
                "query": f"{symbol} stock news",
                "topic": "news",
                "time_range": self.settings.research_time_range,
                "search_depth": "basic",
                "max_results": 6,
                "include_published_date": True,
            },
        )
        resp.raise_for_status()
        out = []
        for r in resp.json().get("results") or []:
            url = str(r.get("url") or "")
            title = str(r.get("title") or "").strip()
            if not url or not title:
                continue
            out.append(Evidence(
                symbol=symbol, provider="tavily", title=title, url=url, domain=_domain(url),
                published=(str(r.get("published_date"))[:10] if r.get("published_date") else None),
                snippet=str(r.get("content") or "")[:500],
            ))
        return out

    async def _parallel(self, client: httpx.AsyncClient, symbol: str) -> list[Evidence]:
        floor = (date.today() - timedelta(days=3)).isoformat()
        resp = await client.post(
            PARALLEL_URL,
            headers={"x-api-key": str(self.settings.parallel_api_key)},
            json={
                "objective": (
                    f"Material news about {symbol} stock from the last 72 hours: earnings, "
                    "guidance, analyst rating changes, regulatory actions, product or supply-chain news."
                ),
                "search_queries": [f"{symbol} stock news", f"{symbol} earnings guidance analyst"],
                "mode": "fast",
                "advanced_settings": {
                    "max_results": 6,
                    "source_policy": {"after_date": floor},
                    "excerpt_settings": {"max_chars_per_result": 600},
                },
            },
        )
        resp.raise_for_status()
        out = []
        for r in resp.json().get("results") or []:
            url = str(r.get("url") or "")
            title = str(r.get("title") or "").strip()
            if not url or not title:
                continue
            excerpts = r.get("excerpts") or []
            out.append(Evidence(
                symbol=symbol, provider="parallel", title=title, url=url, domain=_domain(url),
                published=r.get("publish_date"),
                snippet=(str(excerpts[0]) if excerpts else "")[:500],
            ))
        return out

    async def _collect_async(self, symbols: list[str]) -> tuple[list[Evidence], list[dict[str, str]]]:
        sem = asyncio.Semaphore(MAX_CONCURRENCY)
        errors: list[dict[str, str]] = []

        async def run(name: str, fn, client, symbol):
            async with sem:
                try:
                    return await fn(client, symbol)
                except httpx.HTTPStatusError as exc:
                    errors.append({"provider": name, "symbol": symbol,
                                   "error": f"HTTP {exc.response.status_code}"})
                except (httpx.HTTPError, ValueError) as exc:
                    errors.append({"provider": name, "symbol": symbol, "error": type(exc).__name__})
                return []

        jobs = []
        async with httpx.AsyncClient(timeout=25) as client:
            for symbol in symbols:
                if self.settings.tavily_api_key:
                    jobs.append(run("tavily", self._tavily, client, symbol))
                if self.settings.parallel_api_key:
                    jobs.append(run("parallel", self._parallel, client, symbol))
            batches = await asyncio.gather(*jobs)
        return [e for batch in batches for e in batch], errors

    # -- clustering ----------------------------------------------------------

    @staticmethod
    def cluster(evidence: list[Evidence]) -> list[Claim]:
        """Greedy single-pass clustering by headline overlap, per symbol."""
        claims: list[Claim] = []
        seen_urls: set[str] = set()
        for ev in evidence:
            if ev.url in seen_urls:
                continue
            seen_urls.add(ev.url)
            toks = ev.tokens()
            best, best_score = None, 0.0
            for claim in claims:
                if claim.symbol != ev.symbol:
                    continue
                other = claim.evidence[0].tokens()
                if not toks or not other:
                    continue
                score = len(toks & other) / len(toks | other)
                if score > best_score:
                    best, best_score = claim, score
            if best is not None and best_score >= CLAIM_SIMILARITY:
                best.evidence.append(ev)
            else:
                claims.append(Claim(
                    claim_id=f"c{len(claims) + 1}", symbol=ev.symbol,
                    headline=ev.title, snippet=ev.snippet, evidence=[ev],
                ))
        return claims

    # -- public --------------------------------------------------------------

    def collect(self, symbols: list[str]) -> dict[str, Any]:
        symbols = [s.upper() for s in symbols]
        fetched, errors = asyncio.run(self._collect_async(symbols))
        evidence = [e for e in fetched if is_news(e)]
        claims = self.cluster(evidence)
        by_symbol: dict[str, list[dict[str, Any]]] = {s: [] for s in symbols}
        for claim in claims:
            by_symbol.setdefault(claim.symbol, []).append(claim.to_dict())
        return {
            "mode": "live",
            "providers": self.providers,
            "symbols": symbols,
            "raw_hits": len(evidence),
            "filtered_out": len(fetched) - len(evidence),
            "claim_count": len(claims),
            "claims": by_symbol,
            "errors": errors,
        }
