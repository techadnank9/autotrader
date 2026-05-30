from decimal import Decimal
import unittest

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


if __name__ == "__main__":
    unittest.main()
