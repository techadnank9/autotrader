from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def _decimal_from_env(name: str, default: str) -> Decimal:
    raw = os.environ.get(name, default)
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError(f"{name} must be a decimal-compatible value, got {raw!r}") from exc
    if value <= 0:
        raise ValueError(f"{name} must be greater than zero, got {value}")
    return value


@dataclass(frozen=True)
class Settings:
    codex_bin: str
    robinhood_mcp_url: str
    enable_web_search: bool
    max_budget_usd: Decimal
    default_budget_usd: Decimal
    bright_data_api_key: str | None = None
    bright_data_zone: str = "serp_api1"
    bright_data_endpoint: str = "https://api.brightdata.com/discover"

    @classmethod
    def load(cls, dotenv_path: str = ".env") -> "Settings":
        _load_dotenv(Path(dotenv_path))
        max_budget = _decimal_from_env("MAX_BUDGET_USD", "5")
        default_budget = _decimal_from_env("DEFAULT_BUDGET_USD", "5")
        if default_budget > max_budget:
            raise ValueError("DEFAULT_BUDGET_USD cannot exceed MAX_BUDGET_USD.")

        enable_web_search = os.environ.get("ENABLE_WEB_SEARCH", "true").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        return cls(
            codex_bin=os.environ.get("CODEX_BIN", "codex").strip() or "codex",
            robinhood_mcp_url=os.environ.get(
                "ROBINHOOD_MCP_URL",
                "https://agent.robinhood.com/mcp/trading",
            ).strip(),
            enable_web_search=enable_web_search,
            max_budget_usd=max_budget,
            default_budget_usd=default_budget,
            bright_data_api_key=os.environ.get("BRIGHT_DATA_API_KEY", "").strip() or None,
            bright_data_zone=os.environ.get("BRIGHT_DATA_ZONE", "serp_api1").strip() or "serp_api1",
            bright_data_endpoint=os.environ.get(
                "BRIGHT_DATA_ENDPOINT",
                "https://api.brightdata.com/discover",
            ).strip(),
        )
