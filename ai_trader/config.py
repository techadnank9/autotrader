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
    replay_log_dir: str = ".ai_trader/replay"
    portfolio_agent_dir: str = ".ai_trader/portfolio_agents"
    portfolio_cash_reserve_usd: Decimal = Decimal("5")
    portfolio_max_positions: int = 5
    portfolio_max_position_pct: Decimal = Decimal("0.45")
    portfolio_min_trade_usd: Decimal = Decimal("5")
    portfolio_candidate_pool_size: int = 6
    market_universe_size: int = 50
    market_evidence_symbol_limit: int = 50
    market_yahoo_screener_id: str = "most_actives"
    market_label_horizons: tuple[int, ...] = (1, 5)
    sia_bin: str = "sia"
    sia_task_dir: str = "sia_tasks/portfolio-management-replay"
    sia_meta_profile: str = "sia_profiles/ai-trader-meta.json"
    sia_target_profile: str = "sia_profiles/ai-trader-portfolio.json"
    sia_web_port: int = 8010
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    telegram_webhook_secret: str | None = None
    decision_dir: str = ".ai_trader/decisions"
    decision_ttl_minutes: int = 240
    account_dir: str = ".ai_trader/accounts"
    session_secret: str = "dev-insecure-session-secret"
    session_secure_cookie: bool = False
    database_url: str | None = None

    @classmethod
    def load(cls, dotenv_path: str = ".env") -> "Settings":
        _load_dotenv(Path(dotenv_path))
        max_budget = _decimal_from_env("MAX_BUDGET_USD", "5")
        default_budget = _decimal_from_env("DEFAULT_BUDGET_USD", "5")
        cash_reserve = _nonnegative_decimal_from_env("PORTFOLIO_CASH_RESERVE_USD", "5")
        max_position_pct = _decimal_from_env("PORTFOLIO_MAX_POSITION_PCT", "0.45")
        if max_position_pct > Decimal("1"):
            raise ValueError("PORTFOLIO_MAX_POSITION_PCT cannot exceed 1.")
        min_trade_usd = _nonnegative_decimal_from_env("PORTFOLIO_MIN_TRADE_USD", "5")
        max_positions = _int_from_env("PORTFOLIO_MAX_POSITIONS", "5")
        candidate_pool_size = _int_from_env("PORTFOLIO_CANDIDATE_POOL_SIZE", "6")
        market_universe_size = _int_from_env("MARKET_UNIVERSE_SIZE", "50")
        market_evidence_symbol_limit = _int_from_env("MARKET_EVIDENCE_SYMBOL_LIMIT", "50")
        market_label_horizons = _int_tuple_from_env("MARKET_LABEL_HORIZONS", "1,5")
        sia_web_port = _int_from_env("SIA_WEB_PORT", "8010")
        decision_ttl_minutes = _int_from_env("DECISION_TTL_MINUTES", "240")
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
            bright_data_api_key=(
                os.environ.get("BRIGHT_DATA_API_KEY", "").strip()
                or os.environ.get("BRIGHTDATA_API_KEY", "").strip()
                or None
            ),
            bright_data_zone=os.environ.get("BRIGHT_DATA_ZONE", "serp_api1").strip() or "serp_api1",
            bright_data_endpoint=os.environ.get(
                "BRIGHT_DATA_ENDPOINT",
                "https://api.brightdata.com/discover",
            ).strip(),
            replay_log_dir=os.environ.get("REPLAY_LOG_DIR", ".ai_trader/replay").strip() or ".ai_trader/replay",
            portfolio_agent_dir=os.environ.get("PORTFOLIO_AGENT_DIR", ".ai_trader/portfolio_agents").strip()
            or ".ai_trader/portfolio_agents",
            portfolio_cash_reserve_usd=cash_reserve,
            portfolio_max_positions=max_positions,
            portfolio_max_position_pct=max_position_pct,
            portfolio_min_trade_usd=min_trade_usd,
            portfolio_candidate_pool_size=candidate_pool_size,
            market_universe_size=market_universe_size,
            market_evidence_symbol_limit=market_evidence_symbol_limit,
            market_yahoo_screener_id=os.environ.get("MARKET_YAHOO_SCREENER_ID", "most_actives").strip() or "most_actives",
            market_label_horizons=market_label_horizons,
            sia_bin=_resolve_sia_bin(),
            sia_task_dir=os.environ.get("SIA_TASK_DIR", "sia_tasks/portfolio-management-replay").strip()
            or "sia_tasks/portfolio-management-replay",
            sia_meta_profile=os.environ.get("SIA_META_PROFILE", "sia_profiles/ai-trader-meta.json").strip()
            or "sia_profiles/ai-trader-meta.json",
            sia_target_profile=os.environ.get("SIA_TARGET_PROFILE", "sia_profiles/ai-trader-portfolio.json").strip()
            or "sia_profiles/ai-trader-portfolio.json",
            sia_web_port=sia_web_port,
            telegram_bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", "").strip() or None,
            telegram_chat_id=os.environ.get("TELEGRAM_CHAT_ID", "").strip() or None,
            telegram_webhook_secret=os.environ.get("TELEGRAM_WEBHOOK_SECRET", "").strip() or None,
            decision_dir=os.environ.get("DECISION_DIR", ".ai_trader/decisions").strip()
            or ".ai_trader/decisions",
            decision_ttl_minutes=decision_ttl_minutes,
            account_dir=os.environ.get("ACCOUNT_DIR", ".ai_trader/accounts").strip()
            or ".ai_trader/accounts",
            session_secret=os.environ.get("SESSION_SECRET", "").strip()
            or "dev-insecure-session-secret",
            session_secure_cookie=os.environ.get("SESSION_SECURE_COOKIE", "").strip().lower()
            in {"1", "true", "yes", "on"},
            database_url=os.environ.get("DATABASE_URL", "").strip()
            or os.environ.get("POSTGRES_URL", "").strip()
            or None,
        )

def _nonnegative_decimal_from_env(name: str, default: str) -> Decimal:
    raw = os.environ.get(name, default)
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError(f"{name} must be a decimal-compatible value, got {raw!r}") from exc
    if value < 0:
        raise ValueError(f"{name} must be greater than or equal to zero, got {value}")
    return value


def _int_from_env(name: str, default: str, *, minimum: int = 1) -> int:
    raw = os.environ.get(name, default).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer-compatible value, got {raw!r}") from exc
    if value < minimum:
        raise ValueError(f"{name} must be greater than or equal to {minimum}, got {value}")
    return value


def _int_tuple_from_env(name: str, default: str) -> tuple[int, ...]:
    raw = os.environ.get(name, default).strip()
    values: list[int] = []
    for item in raw.split(","):
        stripped = item.strip()
        if not stripped:
            continue
        try:
            value = int(stripped)
        except ValueError as exc:
            raise ValueError(f"{name} must contain comma-separated integers, got {raw!r}") from exc
        if value < 1:
            raise ValueError(f"{name} entries must be greater than or equal to 1, got {value}")
        values.append(value)
    if not values:
        raise ValueError(f"{name} must contain at least one positive integer.")
    return tuple(values)


def _resolve_sia_bin() -> str:
    configured = os.environ.get("SIA_BIN", "").strip()
    if configured:
        return configured
    local_bin = Path(".sia-venv/bin/sia")
    if local_bin.is_file():
        return str(local_bin.resolve())
    return "sia"
