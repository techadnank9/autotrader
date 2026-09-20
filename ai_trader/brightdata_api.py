"""Bright Data evidence collection for the stock pool.

The engine passes in a ``fetcher`` so this module stays transport-agnostic:
``BrightDataClient._discover`` supplies the real HTTP call, tests supply a stub.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

Fetcher = Callable[[str, str], dict[str, Any]]

SOURCE_QUERIES: dict[str, str] = {
    "reddit": 'site:reddit.com {symbol} stock discussion',
    "x": 'site:x.com {symbol} stock',
    "realtime": '{symbol} stock news today price',
}

MAX_ITEMS_PER_SOURCE = 6


def collect_stock_evidence(
    symbols: Iterable[str],
    *,
    fetcher: Fetcher,
    sources: Iterable[str] = ("reddit", "x", "realtime"),
    max_items: int = MAX_ITEMS_PER_SOURCE,
) -> dict[str, Any]:
    """Collect per-source evidence for each symbol.

    Returns the shape the Codex prompt and the UI already expect:
    ``{"mode": ..., "reddit": [...], "x": [...], "realtime": [...]}``.
    """
    selected = [symbol.strip().upper() for symbol in symbols if symbol and symbol.strip()]
    payload: dict[str, Any] = {
        "mode": "bright_data",
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "symbols": selected,
        "errors": [],
    }
    for source in sources:
        payload[source] = []

    template_missing: list[str] = []
    for source in sources:
        template = SOURCE_QUERIES.get(source)
        if template is None:
            template_missing.append(source)
            continue
        for symbol in selected:
            query = template.format(symbol=symbol)
            try:
                raw = fetcher(query, source)
            except Exception as exc:  # network/transport failures must not kill the pass
                payload["errors"].append({"source": source, "symbol": symbol, "error": str(exc)})
                continue
            if not isinstance(raw, dict):
                raw = {"ok": False, "items": raw}
            if not raw.get("ok", True):
                payload["errors"].append(
                    {
                        "source": source,
                        "symbol": symbol,
                        "error": raw.get("error", "Bright Data returned an error."),
                        "status_code": raw.get("status_code"),
                    }
                )
                continue
            payload[source].extend(
                _normalize_items(raw.get("items"), symbol=symbol, source=source, limit=max_items)
            )

    if template_missing:
        payload["errors"].append({"error": f"Unknown Bright Data sources ignored: {template_missing}"})
    if not any(payload.get(source) for source in sources):
        payload["mode"] = "bright_data_empty"
    return payload


def _normalize_items(raw: Any, *, symbol: str, source: str, limit: int) -> list[dict[str, Any]]:
    """Flatten the assorted shapes Bright Data can return into evidence rows."""
    records = _candidate_records(raw)
    normalized: list[dict[str, Any]] = []
    for record in records:
        if len(normalized) >= limit:
            break
        if not isinstance(record, dict):
            text = str(record).strip()
            if text:
                normalized.append({"symbol": symbol, "source": source, "title": text[:280], "url": None})
            continue
        title = _first_string(record, ("title", "name", "headline", "text", "snippet", "description"))
        if not title:
            continue
        normalized.append(
            {
                "symbol": symbol,
                "source": source,
                "title": title[:280],
                "url": _first_string(record, ("url", "link", "permalink", "source_url")),
                "snippet": (_first_string(record, ("snippet", "description", "text")) or "")[:400] or None,
            }
        )
    return normalized


def _candidate_records(raw: Any) -> list[Any]:
    if raw is None:
        return []
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        for key in ("results", "items", "organic", "organic_results", "data", "posts"):
            value = raw.get(key)
            if isinstance(value, list):
                return value
        return [raw]
    return [raw]


def _first_string(record: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def save_evidence_file(payload: dict[str, Any], path: str | Path) -> Path:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return output
