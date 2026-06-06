from decimal import Decimal
import json
import unittest
from unittest.mock import patch

from ai_trader.config import Settings
from ai_trader.engine import AnalysisRequest, BrightDataClient, RecommendationEngine, SourceWeights


class _FakeResponse:
    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps({"results": [{"title": "Nvidia latest earnings news"}]}).encode("utf-8")


class BrightDataTests(unittest.TestCase):
    def test_discover_payload_shape(self) -> None:
        settings = Settings(
            codex_bin="codex",
            robinhood_mcp_url="https://agent.robinhood.com/mcp/trading",
            enable_web_search=True,
            max_budget_usd=Decimal("5"),
            default_budget_usd=Decimal("5"),
            bright_data_api_key="test-key",
            bright_data_endpoint="https://api.brightdata.com/discover",
        )
        client = BrightDataClient(settings)

        captured = {}

        def fake_urlopen(req, timeout):
            captured["url"] = req.full_url
            captured["payload"] = json.loads(req.data.decode("utf-8"))
            captured["auth"] = req.headers["Authorization"]
            captured["timeout"] = timeout
            return _FakeResponse()

        with patch("ai_trader.engine.request.urlopen", fake_urlopen):
            result = client._discover("Nvidia latest earnings news", source="realtime")

        self.assertTrue(result["ok"])
        self.assertEqual(captured["url"], "https://api.brightdata.com/discover")
        self.assertEqual(captured["payload"]["query"], "Nvidia latest earnings news")
        self.assertEqual(captured["payload"]["mode"], "standard")
        self.assertEqual(captured["payload"]["language"], "en")
        self.assertEqual(captured["payload"]["country"], "US")
        self.assertEqual(captured["payload"]["format"], "json")
        self.assertEqual(captured["payload"]["num_results"], 8)
        self.assertEqual(captured["auth"], "Bearer test-key")

    def test_discover_polls_async_task(self) -> None:
        settings = Settings(
            codex_bin="codex",
            robinhood_mcp_url="https://agent.robinhood.com/mcp/trading",
            enable_web_search=True,
            max_budget_usd=Decimal("5"),
            default_budget_usd=Decimal("5"),
            bright_data_api_key="test-key",
            bright_data_endpoint="https://api.brightdata.com/discover",
        )
        client = BrightDataClient(settings)

        with (
            patch.object(
                client,
                "_get_discover_result",
                return_value={"status": "done", "results": [{"title": "AAPL news"}]},
            ) as get_result,
            patch("ai_trader.engine.time.sleep") as sleep,
        ):
            result = client._poll_discover_task("task-123", query="AAPL latest news", source="realtime")

        self.assertTrue(result["ok"])
        self.assertEqual(result["task_id"], "task-123")
        self.assertEqual(result["items"]["results"][0]["title"], "AAPL news")
        get_result.assert_called_once_with("task-123")
        sleep.assert_not_called()


class AllocationTests(unittest.TestCase):
    def test_pool_allocations_are_normalized_from_scores(self) -> None:
        settings = Settings(
            codex_bin="codex",
            robinhood_mcp_url="https://agent.robinhood.com/mcp/trading",
            enable_web_search=True,
            max_budget_usd=Decimal("5"),
            default_budget_usd=Decimal("5"),
        )
        engine = RecommendationEngine(settings)
        result = engine._normalize_allocations(
            {
                "pool": [
                    {"symbol": "AAPL", "score": 0.4, "allocation_usd": 0},
                    {"symbol": "NVDA", "score": 0.1, "allocation_usd": 0},
                ]
            },
            AnalysisRequest(
                budget=Decimal("5"),
                pool_size=2,
                weights=SourceWeights(reddit=0.3, x=0.3, realtime=0.4),
            ),
        )

        self.assertEqual(result["pool"][0]["allocation_usd"], 4.0)
        self.assertEqual(result["pool"][1]["allocation_usd"], 1.0)

    def test_equal_allocation_preserves_total_budget(self) -> None:
        settings = Settings(
            codex_bin="codex",
            robinhood_mcp_url="https://agent.robinhood.com/mcp/trading",
            enable_web_search=True,
            max_budget_usd=Decimal("50"),
            default_budget_usd=Decimal("5"),
        )
        engine = RecommendationEngine(settings)
        result = engine._normalize_allocations(
            {
                "pool": [
                    {"symbol": "AAPL", "score": 0},
                    {"symbol": "MSFT", "score": 0},
                    {"symbol": "NVDA", "score": 0},
                ]
            },
            AnalysisRequest(
                budget=Decimal("5"),
                pool_size=3,
                weights=SourceWeights(reddit=0.3, x=0.3, realtime=0.4),
            ),
        )

        total = sum(Decimal(str(item["allocation_usd"])) for item in result["pool"])
        self.assertEqual(total, Decimal("5.0"))


class TradeFlowTests(unittest.TestCase):
    def test_trade_prompt_auto_confirms_when_execute_true(self) -> None:
        settings = Settings(
            codex_bin="codex",
            robinhood_mcp_url="https://agent.robinhood.com/mcp/trading",
            enable_web_search=True,
            max_budget_usd=Decimal("5"),
            default_budget_usd=Decimal("5"),
        )
        engine = RecommendationEngine(settings)

        with patch.object(engine, "trade", return_value={"status": "ok"}) as trade:
            result = engine.trade_prompt({"symbol": "AAPL", "dollar_amount": 3}, execute=True)

        self.assertEqual(result["status"], "ok")
        trade.assert_called_once_with(
            {"symbol": "AAPL", "dollar_amount": 3},
            execute=True,
            confirm_phrase="CONFIRM",
        )

    def test_trade_blocks_without_confirm(self) -> None:
        settings = Settings(
            codex_bin="codex",
            robinhood_mcp_url="https://agent.robinhood.com/mcp/trading",
            enable_web_search=True,
            max_budget_usd=Decimal("5"),
            default_budget_usd=Decimal("5"),
        )
        engine = RecommendationEngine(settings)

        result = engine.trade(
            {"symbol": "AAPL", "dollar_amount": 3},
            execute=True,
            confirm_phrase="",
        )

        self.assertEqual(result["status"], "blocked")
        self.assertIn("CONFIRM", result["message"])


class AnalysisTraceTests(unittest.TestCase):
    def test_analyze_includes_reasoning_trace(self) -> None:
        settings = Settings(
            codex_bin="codex",
            robinhood_mcp_url="https://agent.robinhood.com/mcp/trading",
            enable_web_search=True,
            max_budget_usd=Decimal("5"),
            default_budget_usd=Decimal("5"),
        )
        engine = RecommendationEngine(settings)
        request_model = AnalysisRequest(
            budget=Decimal("5"),
            pool_size=3,
            weights=SourceWeights(reddit=0.3, x=0.3, realtime=0.4),
        )

        with patch.object(engine.bright_data, "collect", return_value={"mode": "demo"}), patch.object(
            engine.llm,
            "recommend",
            return_value={
                "pool": [{"symbol": "AAPL", "score": 0.8, "reason": "Strong setup"}],
                "recommendation": {"decision": "buy", "symbol": "AAPL", "dollar_amount": 5, "confidence": 0.7},
                "source_summary": {"reddit": "Positive", "x": "Mixed", "realtime": "Stable"},
            },
        ):
            result = engine.analyze(request_model)

        self.assertTrue(result["reasoning_trace"])
        self.assertEqual(result["reasoning_trace"][-1]["stage"], "decision")


if __name__ == "__main__":
    unittest.main()
