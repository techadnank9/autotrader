"""Daily top-universe market snapshots used as extra replay-learning signal."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib import error, parse, request

from ai_trader.config import Settings
from ai_trader.replay import ReplayStore, YahooEODPriceProvider

SCREENER_URL = "https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved"
USER_AGENT = "ai-trader/0.2 (+market-research)"

FALLBACK_UNIVERSE = [
    "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "AMD", "TSLA", "AVGO", "COST",
    "NFLX", "JPM", "V", "MA", "UNH", "XOM", "JNJ", "PG", "HD", "LLY",
]


class MarketResearchService:
    def __init__(self, settings: Settings, replay: ReplayStore) -> None:
        self.settings = settings
        self.replay = replay
        self.snapshot_dir = replay.market_dir
        self.labels_path = replay.market_dir / "labels.json"

    # -- collection ----------------------------------------------------------

    def collect_daily_snapshot(self, *, force: bool = False) -> dict[str, Any]:
        today = date.today().isoformat()
        snapshot_id = f"market-{today}"
        path = self.snapshot_dir / f"{snapshot_id}.json"

        if path.exists() and not force:
            payload = _read_json(path)
            payload["cached"] = True
            return payload

        symbols, source = self._fetch_universe(self.settings.market_universe_size)
        quotes = self._fetch_quotes(symbols[: self.settings.market_evidence_symbol_limit])

        payload = {
            "snapshot_id": snapshot_id,
            "date": today,
            "collected_at": datetime.now(timezone.utc).isoformat(),
            "universe_source": source,
            "screener_id": self.settings.market_yahoo_screener_id,
            "symbols": symbols,
            "quotes": quotes,
            "label_horizons": list(self.settings.market_label_horizons),
            "cached": False,
        }
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return payload

    def list_snapshots(self) -> list[dict[str, Any]]:
        if not self.snapshot_dir.exists():
            return []
        snapshots = []
        for path in sorted(self.snapshot_dir.glob("market-*.json")):
            payload = _read_json(path)
            if payload.get("snapshot_id"):
                snapshots.append(payload)
        return snapshots

    # -- labeling ------------------------------------------------------------

    def build_labels(self, provider: YahooEODPriceProvider | None = None) -> dict[str, Any]:
        provider = provider or YahooEODPriceProvider()
        labels = _read_json(self.labels_path) if self.labels_path.exists() else {}
        horizons = self.settings.market_label_horizons
        built = 0

        for snapshot in self.list_snapshots():
            snapshot_id = str(snapshot.get("snapshot_id"))
            snapshot_date = _parse_date(snapshot.get("date"))
            if not snapshot_id or snapshot_date is None:
                continue
            existing = labels.get(snapshot_id, {})
            if existing.get("status") == "complete":
                continue

            window_end = min(date.today(), snapshot_date + timedelta(days=max(horizons) + 8))
            per_symbol: dict[str, dict[str, float]] = {}
            incomplete = False
            for symbol in snapshot.get("symbols", [])[: self.settings.market_evidence_symbol_limit]:
                closes = provider.daily_closes(symbol, snapshot_date - timedelta(days=5), window_end)
                horizon_returns: dict[str, float] = {}
                for horizon in horizons:
                    value = _forward_return(closes, snapshot_date, horizon)
                    if value is None:
                        incomplete = True
                        continue
                    horizon_returns[f"d{horizon}"] = value
                if horizon_returns:
                    per_symbol[symbol] = horizon_returns

            if not per_symbol:
                continue

            first_horizon = f"d{horizons[0]}"
            ranked = sorted(
                (symbol for symbol in per_symbol if first_horizon in per_symbol[symbol]),
                key=lambda symbol: per_symbol[symbol][first_horizon],
                reverse=True,
            )
            labels[snapshot_id] = {
                "snapshot_id": snapshot_id,
                "date": snapshot.get("date"),
                "status": "partial" if incomplete else "complete",
                "horizons": list(horizons),
                "returns": per_symbol,
                "top_symbols": ranked[:10],
                "labeled_at": datetime.now(timezone.utc).isoformat(),
            }
            built += 1

        self.labels_path.parent.mkdir(parents=True, exist_ok=True)
        self.labels_path.write_text(json.dumps(labels, indent=2), encoding="utf-8")
        return labels

    # -- fetch helpers -------------------------------------------------------

    def _fetch_universe(self, count: int) -> tuple[list[str], str]:
        params = parse.urlencode(
            {"scrIds": self.settings.market_yahoo_screener_id, "count": max(count, 1), "formatted": "false"}
        )
        payload = _get_json(f"{SCREENER_URL}?{params}")
        try:
            quotes = payload["finance"]["result"][0]["quotes"]
        except (KeyError, IndexError, TypeError):
            quotes = []

        symbols = []
        for quote in quotes:
            symbol = str((quote or {}).get("symbol") or "").strip().upper()
            if symbol and symbol.isalpha() and symbol not in symbols:
                symbols.append(symbol)
        if symbols:
            return symbols[:count], f"yahoo:{self.settings.market_yahoo_screener_id}"
        return FALLBACK_UNIVERSE[:count], "fallback"

    def _fetch_quotes(self, symbols: list[str]) -> dict[str, Any]:
        if not symbols:
            return {}
        provider = YahooEODPriceProvider()
        today = date.today()
        quotes: dict[str, Any] = {}
        for symbol in symbols:
            closes = provider.daily_closes(symbol, today - timedelta(days=10), today)
            if not closes:
                continue
            sessions = sorted(closes)
            last = sessions[-1]
            prior = sessions[-2] if len(sessions) > 1 else None
            quotes[symbol] = {
                "last_close": closes[last],
                "last_session": last,
                "prior_close": closes[prior] if prior else None,
                "session_return": (
                    round((closes[last] - closes[prior]) / closes[prior], 6)
                    if prior and closes[prior] > 0
                    else None
                ),
            }
        return quotes


def _get_json(url: str, *, timeout: int = 20) -> dict[str, Any]:
    req = request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except (error.URLError, error.HTTPError, TimeoutError, json.JSONDecodeError, OSError):
        return {}


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _parse_date(value: Any) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _forward_return(closes: dict[str, float], base_date: date, horizon: int) -> float | None:
    sessions = sorted(closes)
    base_day = None
    for day in sessions:
        if day <= base_date.isoformat():
            base_day = day
        else:
            break
    if base_day is None:
        return None
    future = [day for day in sessions if day > base_day]
    if len(future) < horizon:
        return None
    base = closes[base_day]
    if base <= 0:
        return None
    return round((closes[future[horizon - 1]] - base) / base, 6)
