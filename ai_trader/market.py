"""Market context for a buy decision, and live intraday prices.

Answers the questions a buyer should see before tapping Buy: how did this stock
move today, compared with the market and its sector; how much does it normally
move in a day; and what did it do over the past week and month. Also serves the
intraday series behind the live chart after an order.
"""

from __future__ import annotations

import asyncio
import re
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
    first_open = next((o for o in (quote.get("open") or []) if o is not None), None)
    return {"meta": res.get("meta") or {}, "t": [p[0] for p in pairs], "close": [p[1] for p in pairs],
            "first_open": first_open}


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


# -- search ------------------------------------------------------------------

SEARCH = "https://query1.finance.yahoo.com/v1/finance/search"
US_EXCHANGES = {"NMS", "NGM", "NCM", "NYQ", "ASE", "PCX", "BTS", "NAS", "NYS"}

# Leveraged and inverse funds: kept out of buying to honor "no leverage" (they can
# still be looked at). "Short" alone is too broad (short-term bond funds), so it
# only counts when it is not about duration.
_LEVERAGED = re.compile(
    r"(\b\d(?:\.\d+)?x\b|\bultra(?:pro|short)?\b|\bleveraged?\b|\binverse\b|\bbear\b|"
    r"\bdaily targ|\bshort\b(?![- ](?:term|duration|maturity)))",
    re.IGNORECASE,
)


def is_leveraged_or_inverse(name: str | None) -> bool:
    return bool(name and _LEVERAGED.search(name))


def search(query: str) -> list[dict[str, Any]]:
    q = (query or "").strip()
    if not q:
        return []
    hit = _cached(f"search:{q.lower()}", 600)
    if hit is not None:
        return hit
    try:
        r = httpx.get(SEARCH, params={"q": q, "quotesCount": 12, "newsCount": 0, "listsCount": 0},
                      headers=HEADERS, timeout=10)
        r.raise_for_status()
        quotes = r.json().get("quotes") or []
    except (httpx.HTTPError, ValueError):
        return []
    out = []
    for x in quotes:
        sym = str(x.get("symbol") or "")
        if x.get("quoteType") not in {"EQUITY", "ETF"} or x.get("exchange") not in US_EXCHANGES:
            continue
        if not sym or any(c in sym for c in ".=^"):
            continue
        name = x.get("longname") or x.get("shortname") or sym
        out.append({"symbol": sym.replace("-", "."), "name": name, "type": x["quoteType"].lower(),
                    "exchange": x.get("exchDisp") or x.get("exchange"),
                    "restricted": is_leveraged_or_inverse(name)})
    _cache[f"search:{q.lower()}"] = (time.time(), out[:8])
    return out[:8]


# -- full stock page: history, statistics, fundamentals ------------------------

DATA_URL = "https://data.alpaca.markets/v2"
SEC_UA = {"User-Agent": "AI Trader stock research (mdadnan456@gmail.com)"}

# range -> (yahoo range, yahoo interval, alpaca timeframe, days back)
RANGES: dict[str, tuple[str, str, str, int]] = {
    "1D": ("1d", "5m", "5Min", 1), "5D": ("5d", "30m", "30Min", 7), "1M": ("1mo", "1d", "1Day", 31),
    "3M": ("3mo", "1d", "1Day", 92), "6M": ("6mo", "1d", "1Day", 183), "1Y": ("1y", "1d", "1Day", 366),
    "5Y": ("5y", "1wk", "1Week", 1830),
}


class AlpacaData:
    """Alpaca market data with the user's own keys. The free plan gives real-time
    IEX prices and full consolidated (SIP) history, minus the latest 15 minutes."""

    def __init__(self, key_id: str, secret: str) -> None:
        self.h = {"APCA-API-KEY-ID": key_id, "APCA-API-SECRET-KEY": secret}

    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        r = httpx.get(f"{DATA_URL}{path}", headers=self.h, params=params, timeout=15)
        if r.status_code >= 400:
            raise MarketDataError(f"Alpaca data {r.status_code}")
        return r.json()

    def snapshot(self, symbol: str) -> dict[str, Any]:
        d = self._get("/stocks/snapshots", {"symbols": symbol, "feed": "iex"})
        return d.get(symbol) or {}

    def bars(self, symbol: str, timeframe: str, days: int) -> list[dict[str, Any]]:
        now = datetime.now(NY)
        intraday_today = timeframe == "5Min" and days == 1
        if intraday_today:
            # today's session in real time from IEX
            start = now.replace(hour=4, minute=0, second=0, microsecond=0)
            params = {"symbols": symbol, "timeframe": timeframe, "start": start.isoformat(),
                      "feed": "iex", "limit": 10000, "adjustment": "split"}
        else:
            from datetime import timedelta
            params = {"symbols": symbol, "timeframe": timeframe,
                      "start": (now - timedelta(days=days)).isoformat(),
                      "end": (now - timedelta(minutes=16)).isoformat(),  # free SIP: not the latest 15 min
                      "feed": "sip", "limit": 10000, "adjustment": "split"}
        out: list[dict[str, Any]] = []
        for _ in range(10):
            d = self._get("/stocks/bars", params)
            out.extend((d.get("bars") or {}).get(symbol) or [])
            if not d.get("next_page_token"):
                break
            params["page_token"] = d["next_page_token"]
        return out


def _iso_ts(s: str) -> int:
    return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())


def history(symbol: str, rng: str, alpaca: AlpacaData | None = None) -> dict[str, Any]:
    symbol = symbol.upper()
    rng = rng if rng in RANGES else "1M"
    yr, yi, tf, days = RANGES[rng]
    key = f"hist:{symbol}:{rng}:{'a' if alpaca else 'y'}"
    hit = _cached(key, 30 if rng == "1D" else 300)
    if hit:
        return hit
    data = None
    if alpaca:
        try:
            bars = alpaca.bars(symbol, tf, days)
            if len(bars) >= 2:
                data = {"points": [{"t": _iso_ts(b["t"]), "price": b["c"]} for b in bars],
                        "source": "Alpaca" + (" (real-time IEX)" if rng == "1D" else "")}
        except (httpx.HTTPError, MarketDataError, KeyError, ValueError):
            data = None
    if data is None:
        async def go():
            async with httpx.AsyncClient(timeout=15) as c:
                return await _chart(c, symbol, yr, yi)
        try:
            s = asyncio.run(go())
        except (httpx.HTTPError, MarketDataError) as exc:
            raise MarketDataError(f"No price history for {symbol}.") from exc
        data = {"points": [{"t": t, "price": round(p, 4)} for t, p in zip(s["t"], s["close"])],
                "source": "Yahoo Finance", "previous_close": s["meta"].get("chartPreviousClose")}
    data["range"] = rng
    _cache[key] = (time.time(), data)
    return data


_cik_map: dict[str, int] = {}


def fundamentals(symbol: str) -> dict[str, Any]:
    """Shares outstanding and last fiscal year EPS, straight from SEC filings."""
    symbol = symbol.upper().replace(".", "-")
    hit = _cached(f"sec:{symbol}", 43200)
    if hit is not None:
        return hit
    out: dict[str, Any] = {}
    try:
        if not _cik_map:
            r = httpx.get("https://www.sec.gov/files/company_tickers.json", headers=SEC_UA, timeout=20)
            _cik_map.update({v["ticker"]: v["cik_str"] for v in r.json().values()})
        cik = _cik_map.get(symbol)
        if cik:
            f = httpx.get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json",
                          headers=SEC_UA, timeout=25).json().get("facts", {})
            # the cover-page count first; multi-class companies (META, GOOGL) only file it per class,
            # so fall back to the balance-sheet count, then the quarter's weighted average
            fresh = time.strftime("%Y-%m-%d", time.gmtime(time.time() - 550 * 86400))
            for ns, key in (("dei", "EntityCommonStockSharesOutstanding"), ("us-gaap", "CommonStockSharesOutstanding"),
                            ("us-gaap", "WeightedAverageNumberOfSharesOutstandingBasic")):
                so = [x for x in ((f.get(ns, {}).get(key) or {}).get("units") or {}).get("shares") or [] if x.get("end", "") >= fresh]
                if so:
                    latest = max(so, key=lambda x: (x.get("end", ""), x.get("filed", "")))
                    out["shares_outstanding"] = latest["val"]
                    out["shares_as_of"] = latest.get("end")
                    break
            eps = ((f.get("us-gaap", {}).get("EarningsPerShareDiluted") or {}).get("units") or {}).get("USD/shares") or []
            fy = [x for x in eps if x.get("form") == "10-K" and x.get("fp") == "FY"]
            if fy:
                last = max(fy, key=lambda x: x.get("end", ""))
                out["eps_fy"] = last["val"]
                out["eps_fy_end"] = last.get("end")
    except (httpx.HTTPError, ValueError, KeyError):
        pass
    _cache[f"sec:{symbol}"] = (time.time(), out)
    return out


def stats(symbol: str, alpaca: AlpacaData | None = None) -> dict[str, Any]:
    """The statistics block of the stock page."""
    symbol = symbol.upper()
    async def go():
        async with httpx.AsyncClient(timeout=15) as c:
            return await asyncio.gather(_chart(c, symbol, "1d", "5m"), _chart(c, symbol, "1y", "1d"),
                                        return_exceptions=True)
    today, year = asyncio.run(go())
    if isinstance(today, Exception) and isinstance(year, Exception):
        raise MarketDataError(f"No data for {symbol}.")
    meta = today["meta"] if not isinstance(today, Exception) else year["meta"]
    s: dict[str, Any] = {
        "symbol": symbol,
        "name": meta.get("longName") or meta.get("shortName") or symbol,
        "exchange": meta.get("fullExchangeName"),
        "type": (meta.get("instrumentType") or "").lower(),
        "price": meta.get("regularMarketPrice"),
        "previous_close": meta.get("chartPreviousClose") if not isinstance(today, Exception) else None,
        "day_high": meta.get("regularMarketDayHigh"), "day_low": meta.get("regularMarketDayLow"),
        "volume": meta.get("regularMarketVolume"),
        "week52_high": meta.get("fiftyTwoWeekHigh"), "week52_low": meta.get("fiftyTwoWeekLow"),
        "market_open": market_open(meta),
        "price_source": "Yahoo Finance",
    }
    if not isinstance(today, Exception):
        s["open"] = today.get("first_open")  # the session's opening price, not a bar's close
    if not isinstance(year, Exception):
        c = year["close"]
        vols = []
        s["avg_volume"] = None
        try:
            r = httpx.get(f"{CHART}{symbol}", params={"range": "3mo", "interval": "1d"}, headers=HEADERS, timeout=15)
            q = r.json()["chart"]["result"][0]["indicators"]["quote"][0]
            vols = [v for v in (q.get("volume") or []) if v]
            s["avg_volume"] = round(sum(vols) / len(vols)) if vols else None
        except (httpx.HTTPError, KeyError, IndexError, ValueError):
            pass
        if len(c) >= 2:
            s["year_change_pct"] = _pct(c[-1], c[0])

    if alpaca:  # prefer Alpaca for the live price and today's bar when connected
        try:
            snap = alpaca.snapshot(symbol)
            lt, db, pdb = snap.get("latestTrade") or {}, snap.get("dailyBar") or {}, snap.get("prevDailyBar") or {}
            if lt.get("p"):
                s["price"] = lt["p"]
                s["price_source"] = "Alpaca (real-time IEX)"
            if db:
                s.update(open=db.get("o", s.get("open")), day_high=db.get("h", s["day_high"]), day_low=db.get("l", s["day_low"]))
            if pdb.get("c"):
                s["previous_close"] = pdb["c"]
        except (httpx.HTTPError, MarketDataError, ValueError):
            pass

    if s.get("price") and s.get("previous_close"):
        s["change"] = round(s["price"] - s["previous_close"], 4)
        s["change_pct"] = _pct(s["price"], s["previous_close"])
    f = fundamentals(symbol)
    if f.get("shares_outstanding") and s.get("price"):
        s["market_cap"] = s["price"] * f["shares_outstanding"]
    if f.get("eps_fy") is not None:
        s["eps_fy"] = f["eps_fy"]
        if s.get("price") and f["eps_fy"] > 0:  # P/E is meaningless on a loss
            s["pe_fy"] = round(s["price"] / f["eps_fy"], 1)
    s["fundamentals_source"] = "SEC filings" if f else None
    return s
