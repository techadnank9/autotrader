"""Market context for a buy decision, and live intraday prices.

Answers the questions a buyer should see before tapping Buy: how did this stock
move today, compared with the market and its sector; how much does it normally
move in a day; and what did it do over the past week and month. Also serves the
intraday series behind the live chart after an order.
"""

from __future__ import annotations

import asyncio
import statistics
import time
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx

CHART = "https://query1.finance.yahoo.com/v8/finance/chart/"
NY = ZoneInfo("America/New_York")
HEADERS = {"User-Agent": "Mozilla/5.0 (ai-trader)"}

SECTORS: dict[str, tuple[str, str]] = {
    "AAPL": ("XLK", "Technology"), "MSFT": ("XLK", "Technology"), "NVDA": ("XLK", "Technology"),
    "AVGO": ("XLK", "Technology"), "AMD": ("XLK", "Technology"),
    "GOOGL": ("XLC", "Communication services"), "META": ("XLC", "Communication services"),
    "AMZN": ("XLY", "Consumer discretionary"), "TSLA": ("XLY", "Consumer discretionary"),
    "COST": ("XLP", "Consumer staples"),
}

_cache: dict[str, tuple[float, Any]] = {}


def _cached(key: str, ttl: float):
    hit = _cache.get(key)
    return hit[1] if hit and time.time() - hit[0] < ttl else None


class MarketDataError(RuntimeError):
    pass


async def _chart(client: httpx.AsyncClient, symbol: str, rng: str, interval: str) -> dict[str, Any]:
    r = await client.get(f"{CHART}{symbol}", params={"range": rng, "interval": interval}, headers=HEADERS)
    r.raise_for_status()
    result = (r.json().get("chart") or {}).get("result") or []
    if not result:
        raise MarketDataError(f"No market data for {symbol}.")
    res = result[0]
    quote = ((res.get("indicators") or {}).get("quote") or [{}])[0]
    pairs = [(t, c) for t, c in zip(res.get("timestamp") or [], quote.get("close") or []) if c is not None]
    return {"meta": res.get("meta") or {}, "t": [p[0] for p in pairs], "close": [p[1] for p in pairs]}


def _pct(a: float, b: float) -> float | None:
    return round((a / b - 1) * 100, 2) if a and b else None


def _day_label(ts: int) -> str:
    d = datetime.fromtimestamp(ts, NY).date()
    return "Today" if d == datetime.now(NY).date() else d.strftime("%A")


def market_open(meta: dict[str, Any]) -> bool:
    reg = (meta.get("currentTradingPeriod") or {}).get("regular") or {}
    return bool(reg) and reg.get("start", 0) <= time.time() < reg.get("end", 0)


def _day_change(series: dict[str, Any]) -> float | None:
    c = series["close"]
    return _pct(c[-1], c[-2]) if len(c) >= 2 else None


async def _context(symbol: str) -> dict[str, Any]:
    etf, sector = SECTORS.get(symbol, (None, None))
    async with httpx.AsyncClient(timeout=15) as client:
        jobs = [_chart(client, symbol, "3mo", "1d"), _chart(client, "SPY", "5d", "1d")]
        if etf:
            jobs.append(_chart(client, etf, "5d", "1d"))
        results = await asyncio.gather(*jobs, return_exceptions=True)
    stock = results[0]
    if isinstance(stock, Exception):
        raise MarketDataError(f"Could not load prices for {symbol}.")
    spy = results[1] if not isinstance(results[1], Exception) else None
    sec = results[2] if etf and not isinstance(results[2], Exception) else None

    c = stock["close"]
    returns = [(c[i] / c[i - 1] - 1) * 100 for i in range(max(1, len(c) - 20), len(c))]
    meta = stock["meta"]
    return {
        "symbol": symbol,
        "name": meta.get("longName") or meta.get("shortName") or symbol,
        "price": round(float(meta.get("regularMarketPrice") or c[-1]), 2),
        "day_label": _day_label(stock["t"][-1]) if stock["t"] else None,
        "day_change_pct": _day_change(stock),
        "market_change_pct": _day_change(spy) if spy else None,
        "sector": sector,
        "sector_change_pct": _day_change(sec) if sec else None,
        "typical_move_pct": round(statistics.pstdev(returns), 2) if len(returns) >= 5 else None,
        "week_pct": _pct(c[-1], c[-6]) if len(c) >= 6 else None,
        "month_pct": _pct(c[-1], c[-22]) if len(c) >= 22 else None,
        "market_open": market_open(meta),
    }


def context(symbol: str) -> dict[str, Any]:
    symbol = symbol.upper()
    hit = _cached(f"ctx:{symbol}", 300)
    if hit:
        return hit
    try:
        data = asyncio.run(_context(symbol))
    except httpx.HTTPError as exc:
        raise MarketDataError(f"Could not load prices for {symbol}.") from exc
    _cache[f"ctx:{symbol}"] = (time.time(), data)
    return data


def intraday(symbol: str) -> dict[str, Any]:
    """Today's session in 1-minute steps, for the live chart after an order."""
    symbol = symbol.upper()
    hit = _cached(f"intra:{symbol}", 15)
    if hit:
        return hit

    async def go():
        async with httpx.AsyncClient(timeout=15) as client:
            return await _chart(client, symbol, "1d", "1m")

    try:
        s = asyncio.run(go())
    except (httpx.HTTPError, MarketDataError) as exc:
        raise MarketDataError(f"Could not load live prices for {symbol}.") from exc
    meta = s["meta"]
    data = {
        "symbol": symbol,
        "points": [{"t": t, "price": round(p, 4)} for t, p in zip(s["t"], s["close"])],
        "previous_close": meta.get("chartPreviousClose") or meta.get("previousClose"),
        "price": meta.get("regularMarketPrice"),
        "market_open": market_open(meta),
        "session_date": datetime.fromtimestamp(s["t"][-1], NY).strftime("%A, %b %-d") if s["t"] else None,
    }
    _cache[f"intra:{symbol}"] = (time.time(), data)
    return data
