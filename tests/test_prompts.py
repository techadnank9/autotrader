from decimal import Decimal
import json
import unittest

from ai_trader.config import Settings
from ai_trader.prompts import direct_search_prompt, live_prompt, recommendation_prompt
from ai_trader.robinhood import RobinhoodTrader
from tempfile import TemporaryDirectory


class PromptTests(unittest.TestCase):
    def test_recommendation_prompt_mentions_budget(self) -> None:
        prompt = recommendation_prompt("Buy one stock", Decimal("5"))
        self.assertIn("$5.00", prompt)
        self.assertIn('"decision"', prompt)

    def test_live_prompt_mentions_confirm_gate(self) -> None:
        prompt = live_prompt("Buy one stock", Decimal("5"))
        self.assertIn("CONFIRM", prompt)
        self.assertIn("place_equity_order", prompt)

    def test_direct_search_prompt_mentions_query(self) -> None:
        prompt = direct_search_prompt("AAPL")
        self.assertIn("Call `search` for AAPL", prompt)
        self.assertIn('"query": "AAPL"', prompt)

    def test_extract_final_agent_text(self) -> None:
        with TemporaryDirectory() as tmpdir:
            trader = RobinhoodTrader(
                Settings(
                    codex_bin="codex",
                    robinhood_mcp_url="https://agent.robinhood.com/mcp/trading",
                    enable_web_search=True,
                    max_budget_usd=Decimal("5"),
                    default_budget_usd=Decimal("5"),
                )
            )
            stdout = "\n".join(
                [
                    json.dumps({"type": "thread.started"}),
                    json.dumps(
                        {
                            "type": "item.completed",
                            "item": {"type": "agent_message", "text": "{\"query\":\"AAPL\",\"results\":[]}"},
                        }
                    ),
                ]
            )
            self.assertEqual(
                trader._extract_final_agent_text(stdout),
                "{\"query\":\"AAPL\",\"results\":[]}",
            )


if __name__ == "__main__":
    unittest.main()
