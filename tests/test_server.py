from fastapi.testclient import TestClient
import unittest

from ai_trader.server import app


class ServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)

    def test_health_and_index(self) -> None:
        self.assertEqual(self.client.get("/api/health").status_code, 200)
        self.assertEqual(self.client.get("/").status_code, 200)

    def test_trade_defaults_to_no_op(self) -> None:
        response = self.client.post(
            "/api/trade",
            json={
                "recommendation": {"symbol": "AAPL", "dollar_amount": 5},
                "execute": False,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "no_op")


if __name__ == "__main__":
    unittest.main()
