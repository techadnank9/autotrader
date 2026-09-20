"""Management-run logging, next-day labeling, and SIA replay export.

Each ``manage portfolio`` pass writes one run file. Once the next session's EOD
prices are available, ``build_labels`` attaches forward returns so the runs can
be replayed offline as a supervised task. This is offline evaluation data, not
brokerage truth.
"""

from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Protocol
from urllib import error, parse, request

RUNS_DIR = "runs"
LABELS_FILE = "labels.json"
MARKET_DIR = "market"
USER_AGENT = "ai-trader/0.2 (+replay-labeler)"


class PriceProvider(Protocol):
    def daily_closes(self, symbol: str, start: date, end: date) -> dict[str, float]:
        ...


class YahooEODPriceProvider:
    """Daily closes from Yahoo's public chart endpoint."""

    BASE = "https://query1.finance.yahoo.com/v8/finance/chart/"

    def __init__(self, *, timeout: int = 20) -> None:
        self.timeout = timeout
        self._cache: dict[tuple[str, str, str], dict[str, float]] = {}

    def daily_closes(self, symbol: str, start: date, end: date) -> dict[str, float]:
        key = (symbol.upper(), start.isoformat(), end.isoformat())
        if key in self._cache:
            return self._cache[key]

        params = parse.urlencode(
            {
                "period1": int(datetime.combine(start, datetime.min.time(), timezone.utc).timestamp()),
                "period2": int(datetime.combine(end + timedelta(days=1), datetime.min.time(), timezone.utc).timestamp()),
                "interval": "1d",
                "includePrePost": "false",
            }
        )
        req = request.Request(
            f"{self.BASE}{parse.quote(symbol.upper())}?{params}",
            headers={"User-Agent": USER_AGENT},
        )
        try:
            with request.urlopen(req, timeout=self.timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8", errors="replace"))
        except (error.URLError, error.HTTPError, TimeoutError, json.JSONDecodeError, OSError):
            self._cache[key] = {}
            return {}

        closes: dict[str, float] = {}
        try:
            result = payload["chart"]["result"][0]
            timestamps = result.get("timestamp") or []
            quote_closes = result["indicators"]["quote"][0].get("close") or []
        except (KeyError, IndexError, TypeError):
            self._cache[key] = {}
            return {}

        for timestamp, close in zip(timestamps, quote_closes):
            if close is None:
                continue
            day = datetime.fromtimestamp(timestamp, tz=timezone.utc).date().isoformat()
            closes[day] = float(close)
        self._cache[key] = closes
        return closes


class ReplayStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.runs_dir = self.root / RUNS_DIR
        self.market_dir = self.root / MARKET_DIR
        self.labels_path = self.root / LABELS_FILE

    # -- writing -------------------------------------------------------------

    def log_management_run(self, payload: dict[str, Any]) -> str:
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        run_id = payload.get("run_id") or f"run-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:6]}"
        record = {
            "run_id": run_id,
            "logged_at": datetime.now(timezone.utc).isoformat(),
            "trade_date": date.today().isoformat(),
            **payload,
        }
        record["run_id"] = run_id
        (self.runs_dir / f"{run_id}.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
        return run_id

    # -- reading -------------------------------------------------------------

    def list_runs(self) -> list[dict[str, Any]]:
        if not self.runs_dir.exists():
            return []
        runs: list[dict[str, Any]] = []
        for path in sorted(self.runs_dir.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                runs.append(payload)
        runs.sort(key=lambda item: str(item.get("logged_at", "")))
        return runs

    def load_labels(self) -> dict[str, Any]:
        if not self.labels_path.exists():
            return {}
        try:
            payload = json.loads(self.labels_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
        return payload if isinstance(payload, dict) else {}

    def _save_labels(self, labels: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.labels_path.write_text(json.dumps(labels, indent=2), encoding="utf-8")

    # -- labeling ------------------------------------------------------------

    def build_labels(self, provider: PriceProvider, *, horizon_days: int = 1) -> dict[str, Any]:
        """Attach next-session forward returns to every unlabeled run."""
        labels = self.load_labels()
        built = 0
        pending = 0

        for run in self.list_runs():
            run_id = str(run.get("run_id") or "")
            if not run_id or run_id in labels:
                continue
            trade_date = _parse_date(run.get("trade_date") or run.get("logged_at"))
            if trade_date is None:
                continue

            symbols = _run_symbols(run)
            if not symbols:
                labels[run_id] = {
                    "run_id": run_id,
                    "trade_date": trade_date.isoformat(),
                    "status": "no_symbols",
                    "returns": {},
                }
                built += 1
                continue

            window_end = trade_date + timedelta(days=horizon_days + 6)
            if window_end > date.today():
                window_end = date.today()

            returns: dict[str, Any] = {}
            incomplete = False
            for symbol in symbols:
                closes = provider.daily_closes(symbol, trade_date - timedelta(days=5), window_end)
                forward = _forward_return(closes, trade_date, horizon_days)
                if forward is None:
                    incomplete = True
                    continue
                returns[symbol] = forward

            if incomplete and not returns:
                pending += 1
                continue

            labels[run_id] = {
                "run_id": run_id,
                "trade_date": trade_date.isoformat(),
                "horizon_days": horizon_days,
                "status": "partial" if incomplete else "complete",
                "returns": returns,
                "best_symbol": max(returns, key=lambda key: returns[key]) if returns else None,
                "labeled_at": datetime.now(timezone.utc).isoformat(),
            }
            built += 1

        self._save_labels(labels)
        return {"labels_built": built, "pending_labels": pending, "total_labels": len(labels)}

    # -- export --------------------------------------------------------------

    def export_sia_replay_dataset(self, task_dir: str | Path) -> dict[str, Any]:
        """Write the replay task scaffold SIA reads."""
        task_path = Path(task_dir)
        data_dir = task_path / "data"
        data_dir.mkdir(parents=True, exist_ok=True)

        labels = self.load_labels()
        runs = self.list_runs()
        cases: list[dict[str, Any]] = []
        for run in runs:
            run_id = str(run.get("run_id") or "")
            label = labels.get(run_id)
            if not label or not label.get("returns"):
                continue
            cases.append(
                {
                    "case_id": run_id,
                    "trade_date": label.get("trade_date"),
                    "input": {
                        "account_snapshot": run.get("account_snapshot", {}),
                        "ranking": run.get("ranking", []),
                        "policy": run.get("policy", {}),
                    },
                    "recorded_plan": run.get("management_plan", {}),
                    "label": {
                        "returns": label.get("returns", {}),
                        "best_symbol": label.get("best_symbol"),
                        "horizon_days": label.get("horizon_days", 1),
                    },
                }
            )

        replay_path = data_dir / "replay.jsonl"
        with replay_path.open("w", encoding="utf-8") as handle:
            for case in cases:
                handle.write(json.dumps(case) + "\n")

        (task_path / "task.json").write_text(
            json.dumps(
                {
                    "name": "portfolio-management-replay",
                    "description": (
                        "Replay logged Robinhood Agentic portfolio-management runs and score the plan "
                        "against next-session forward returns."
                    ),
                    "entrypoint": "target_agent.py",
                    "data": "data/replay.jsonl",
                    "metric": "top1_accuracy",
                    "case_count": len(cases),
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                },
                indent=2,
            ),
            encoding="utf-8",
        )

        pending = sum(1 for run in runs if str(run.get("run_id")) not in labels)
        return {
            "status": "completed",
            "task_dir": str(task_path),
            "data_file": str(replay_path),
            "cases": len(cases),
            "runs": len(runs),
            "labels": len(labels),
            "pending_labels": pending,
        }


def _run_symbols(run: dict[str, Any]) -> list[str]:
    symbols: list[str] = []
    plan = run.get("management_plan") or {}
    for action in (plan.get("actions") or []):
        if isinstance(action, dict):
            symbol = str(action.get("symbol") or "").strip().upper()
            if symbol and symbol not in symbols:
                symbols.append(symbol)
    for item in (run.get("ranking") or []):
        if isinstance(item, dict):
            symbol = str(item.get("symbol") or "").strip().upper()
            if symbol and symbol not in symbols:
                symbols.append(symbol)
    return symbols


def _forward_return(closes: dict[str, float], trade_date: date, horizon_days: int) -> float | None:
    sessions = sorted(closes)
    base_day = None
    for day in sessions:
        if day <= trade_date.isoformat():
            base_day = day
        else:
            break
    if base_day is None:
        return None
    future = [day for day in sessions if day > base_day]
    if len(future) < horizon_days:
        return None
    base = closes[base_day]
    target = closes[future[horizon_days - 1]]
    if base <= 0:
        return None
    return round((target - base) / base, 6)


def _parse_date(value: Any) -> date | None:
    if not value:
        return None
    text = str(value)
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def iter_dates(values: Iterable[Any]) -> list[date]:
    parsed = [_parse_date(value) for value in values]
    return [item for item in parsed if item is not None]
