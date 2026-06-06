from decimal import Decimal
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from ai_trader.config import Settings
from ai_trader.robinhood import RobinhoodTrader


def _settings() -> Settings:
    return Settings(
        codex_bin="codex",
        robinhood_mcp_url="https://agent.robinhood.com/mcp/trading",
        enable_web_search=True,
        max_budget_usd=Decimal("5"),
        default_budget_usd=Decimal("5"),
    )


class PlanTests(unittest.TestCase):
    def test_settings_load_prefers_repo_local_sia_bin(self) -> None:
        with TemporaryDirectory() as tmpdir:
            local_bin = Path(tmpdir) / ".sia-venv/bin/sia"
            local_bin.parent.mkdir(parents=True, exist_ok=True)
            local_bin.write_text("#!/bin/sh\n", encoding="utf-8")
            with patch.dict(os.environ, {}, clear=True):
                old_cwd = Path.cwd()
                try:
                    os.chdir(tmpdir)
                    settings = Settings.load(dotenv_path="missing.env")
                finally:
                    os.chdir(old_cwd)
        self.assertEqual(settings.sia_bin, str(local_bin.resolve()))

    def test_build_preview_plan_contains_review(self) -> None:
        trader = RobinhoodTrader(_settings())
        plan = trader.build_plan("preview", "Buy one stock", Decimal("5"))
        self.assertEqual(plan.command[0], "codex")
        self.assertIn("--search", plan.command)
        self.assertIn("review_equity_order", plan.prompt)

    def test_build_search_plan_uses_direct_prompt(self) -> None:
        trader = RobinhoodTrader(_settings())
        plan = trader.build_search_plan("AAPL")
        self.assertEqual(plan.command, ["codex", plan.prompt])
        self.assertIn("Call `search` for AAPL", plan.prompt)

    def test_build_search_exec_plan_uses_codex_exec(self) -> None:
        trader = RobinhoodTrader(_settings())
        plan = trader.build_search_exec_plan("AAPL")
        self.assertEqual(
            plan.command[:4],
            ["codex", "exec", "--skip-git-repo-check", "--json"],
        )

    def test_settings_load_parses_market_label_horizons(self) -> None:
        with patch.dict(
            os.environ,
            {
                "MAX_BUDGET_USD": "5",
                "DEFAULT_BUDGET_USD": "5",
                "MARKET_LABEL_HORIZONS": "1, 5,10",
            },
            clear=True,
        ):
            settings = Settings.load(dotenv_path="missing.env")
        self.assertEqual(settings.market_label_horizons, (1, 5, 10))


if __name__ == "__main__":
    unittest.main()
