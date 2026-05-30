from decimal import Decimal
import json
import unittest
from unittest.mock import patch

from ai_trader.config import Settings
from ai_trader.engine import BrightDataClient


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
        self.assertEqual(captured["auth"], "Bearer test-key")


if __name__ == "__main__":
    unittest.main()
