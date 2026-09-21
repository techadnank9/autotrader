from fastapi.testclient import TestClient
import unittest
from unittest.mock import MagicMock, patch

from ai_trader.server import app


class ServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)

    def test_health_and_index(self) -> None:
        self.assertEqual(self.client.get("/api/health").status_code, 200)
        self.assertEqual(self.client.get("/").status_code, 200)
        config = self.client.get("/api/config")
        self.assertEqual(config.status_code, 200)
        self.assertIn("max_budget_usd", config.json())

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

    def test_manage_portfolio_endpoint(self) -> None:
        with patch(
            "ai_trader.server.portfolio_engine.manage_portfolio",
            return_value={"management_plan": {"decision": "manage"}, "execution": {"status": "submitted"}},
        ) as manage_portfolio:
            response = self.client.post(
                "/api/manage-portfolio",
                json={"pool_size": 4, "weights": {"reddit": 0.2, "x": 0.3, "realtime": 0.5}, "use_active_agent": False},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["execution"]["status"], "submitted")
        manage_portfolio.assert_called_once()

    def test_portfolio_snapshot_never_falls_back_to_operator_account(self) -> None:
        # Multi-user rule: a user without their own broker gets an empty snapshot.
        # The local Codex/Robinhood session is one operator's account and must never
        # be shown to other signed-in users.
        with patch("ai_trader.server.trader.fetch_portfolio_snapshot") as operator_fetch:
            response = self.client.get("/api/portfolio-snapshot")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["account_snapshot"]["source"], "none")
        self.assertEqual(response.json()["account_snapshot"]["positions"], [])
        operator_fetch.assert_not_called()

    def test_portfolio_snapshot_uses_the_users_own_broker(self) -> None:
        fake_broker = MagicMock()
        fake_broker.configured = True
        fake_broker.name = "alpaca"
        fake_broker.account.return_value = {"total_value": 10, "buying_power": 5, "cash_available": 5}
        fake_broker.positions.return_value = [{"symbol": "AAPL", "market_value": 3.2, "quantity": 0.01}]
        fake_user = MagicMock(user_id="usr_1", is_demo=False)
        with (
            patch("ai_trader.server._current_user", return_value=fake_user),
            patch("ai_trader.server._broker_for_user", return_value=fake_broker) as broker_for,
        ):
            response = self.client.get("/api/portfolio-snapshot")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["account_snapshot"]["portfolio"]["total_value"], 10)
        broker_for.assert_called_once_with("usr_1")

    def test_portfolio_agent_endpoints(self) -> None:
        listed_payload = {"current": "builtin-default-v1", "previous": None, "eligible": []}
        activated_payload = {"current": "sia-gen-1", "previous": "builtin-default-v1", "eligible": []}
        rolled_back_payload = {"current": "builtin-default-v1", "previous": "sia-gen-1", "eligible": []}
        with (
            patch("ai_trader.server.portfolio_engine.registry.list_agents", return_value=listed_payload),
            patch("ai_trader.server.portfolio_engine.registry.activate", return_value=activated_payload) as activate,
            patch("ai_trader.server.portfolio_engine.registry.rollback", return_value=rolled_back_payload) as rollback,
        ):
            listed = self.client.get("/api/portfolio-agents")
            activated = self.client.post("/api/portfolio-agents/activate", json={"version_id": "sia-gen-1"})
            rolled_back = self.client.post("/api/portfolio-agents/rollback")

        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.json()["current"], "builtin-default-v1")
        self.assertEqual(activated.status_code, 200)
        self.assertEqual(activated.json()["current"], "sia-gen-1")
        self.assertEqual(rolled_back.status_code, 200)
        self.assertEqual(rolled_back.json()["current"], "builtin-default-v1")
        activate.assert_called_once_with("sia-gen-1")
        rollback.assert_called_once_with()

    def test_sia_endpoints(self) -> None:
        status_payload = {"installed": False, "binary": "sia"}
        build_payload = {"status": "completed", "task_dir": "sia_tasks/portfolio-management-replay", "labels_built": 0}
        run_payload = {"status": "completed", "returncode": 0}
        with patch("ai_trader.server.sia_service.status", return_value=status_payload), patch(
            "ai_trader.server.sia_service.build_replay_dataset", return_value=build_payload
        ), patch("ai_trader.server.sia_service.reasoning_trace", return_value=[{"stage": "sia"}]), patch(
            "ai_trader.server.sia_service.run", return_value=run_payload
        ) as run:
            status = self.client.get("/api/sia/status")
            build = self.client.post("/api/sia/replay-build")
            run_response = self.client.post("/api/sia/run", json={"max_generations": 1, "build_replay_first": True})

        self.assertEqual(status.status_code, 200)
        self.assertFalse(status.json()["installed"])
        self.assertEqual(build.status_code, 200)
        self.assertEqual(build.json()["status"], "completed")
        self.assertEqual(run_response.status_code, 200)
        self.assertEqual(run_response.json()["returncode"], 0)
        run.assert_called_once_with(max_generations=1, build_replay_first=True)


if __name__ == "__main__":
    unittest.main()
