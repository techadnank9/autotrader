from __future__ import annotations

import argparse
import json
from decimal import Decimal, InvalidOperation

from ai_trader.agent_runtime import PortfolioAgentRegistry
from ai_trader.config import Settings
from ai_trader.telegram import TelegramClient
from ai_trader.market_research import MarketResearchService
from ai_trader.replay import ReplayStore, YahooEODPriceProvider
from ai_trader.robinhood import RobinhoodTrader, print_query_result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Codex + Robinhood MCP stock trader.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="Launch an interactive Codex trading session.")
    run_parser.add_argument(
        "--mode",
        choices=["recommend", "preview", "live"],
        default="recommend",
        help="How far the Codex prompt should go.",
    )
    run_parser.add_argument(
        "--budget",
        default=None,
        help="Dollar budget, capped by MAX_BUDGET_USD.",
    )
    run_parser.add_argument(
        "--objective",
        default="Recommend one stock to buy today for about $5 in my Robinhood Agentic account.",
        help="Trading objective for the Codex prompt.",
    )
    run_parser.add_argument(
        "--print-only",
        action="store_true",
        help="Print the generated Codex command and prompt without launching Codex.",
    )

    prompt_parser = subparsers.add_parser("prompt", help="Print the generated Codex prompt only.")
    prompt_parser.add_argument("--mode", choices=["recommend", "preview", "live"], default="recommend")
    prompt_parser.add_argument("--budget", default=None)
    prompt_parser.add_argument(
        "--objective",
        default="Recommend one stock to buy today for about $5 in my Robinhood Agentic account.",
    )

    query_parser = subparsers.add_parser(
        "query",
        help="Launch Codex with a direct Robinhood MCP search prompt.",
    )
    query_parser.add_argument(
        "--query",
        default="AAPL",
        help="Search text to send to Robinhood MCP.",
    )
    query_parser.add_argument(
        "--interactive",
        action="store_true",
        help="Launch interactive Codex instead of capturing codex exec output in Python.",
    )
    query_parser.add_argument(
        "--print-only",
        action="store_true",
        help="Print the generated Codex command and prompt without launching Codex.",
    )

    subparsers.add_parser("doctor", help="Inspect Codex and Robinhood MCP readiness.")

    replay_parser = subparsers.add_parser("replay-build", help="Label manage runs and export a SIA replay dataset.")
    replay_parser.add_argument(
        "--task-dir",
        default="sia_tasks/portfolio-management-replay",
        help="Output task directory for SIA replay data.",
    )

    market_parser = subparsers.add_parser("market-collect", help="Collect the daily top-universe market snapshot for replay learning.")
    market_parser.add_argument("--force", action="store_true", help="Refresh today's snapshot even if one is already cached.")

    subparsers.add_parser("agents-list", help="List active and eligible portfolio-manager versions.")

    activate_parser = subparsers.add_parser("agents-activate", help="Activate a passed portfolio-manager version.")
    activate_parser.add_argument("version_id")

    subparsers.add_parser("agents-rollback", help="Rollback to the previously active portfolio-manager version.")

    subparsers.add_parser("telegram-status", help="Check the Telegram bot and webhook registration.")

    hook_parser = subparsers.add_parser("telegram-webhook", help="Register the Telegram callback webhook.")
    hook_parser.add_argument("base_url", help="Public HTTPS base URL, e.g. https://pilottrader.vercel.app")
    hook_parser.add_argument("--delete", action="store_true", help="Remove the webhook instead of setting it.")

    register_parser = subparsers.add_parser(
        "agents-register",
        help="Register a SIA-generated portfolio-manager artifact so it becomes selectable in the app.",
    )
    register_parser.add_argument("version_id")
    register_parser.add_argument("target_agent_path")
    register_parser.add_argument("--label", default=None, help="Human-friendly label for the generated manager.")
    register_parser.add_argument("--source-run", default="manual", help="SIA run identifier or source label.")
    register_parser.add_argument("--status", choices=["passed", "pending", "failed"], default="passed")
    register_parser.add_argument("--metrics-json", default="{}", help="JSON object with evaluation metrics.")
    return parser.parse_args()


def _parse_budget(raw: str | None, settings: Settings) -> Decimal:
    if raw is None:
        return settings.default_budget_usd
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError(f"Invalid budget value: {raw!r}") from exc
    if value <= 0:
        raise ValueError("Budget must be greater than zero.")
    if value > settings.max_budget_usd:
        raise ValueError(
            f"Budget {value} exceeds MAX_BUDGET_USD {settings.max_budget_usd}."
        )
    return value


def _print_plan(command: list[str], prompt: str) -> None:
    print(json.dumps({"command": command, "prompt": prompt}, indent=2))


def main() -> int:
    args = parse_args()
    try:
        settings = Settings.load()
        trader = RobinhoodTrader(settings)

        if args.command == "doctor":
            print(json.dumps(trader.doctor(), indent=2))
            return 0

        if args.command == "replay-build":
            replay = ReplayStore(settings.replay_log_dir)
            market = MarketResearchService(settings, replay)
            snapshot = market.collect_daily_snapshot()
            replay.build_labels(YahooEODPriceProvider())
            market_labels = market.build_labels()
            exported = replay.export_sia_replay_dataset(args.task_dir)
            print(json.dumps({**exported, "market_snapshot_id": snapshot.get("snapshot_id"), "market_labels": len(market_labels)}, indent=2))
            return 0

        if args.command == "market-collect":
            replay = ReplayStore(settings.replay_log_dir)
            market = MarketResearchService(settings, replay)
            print(json.dumps(market.collect_daily_snapshot(force=args.force), indent=2))
            return 0

        if args.command == "telegram-status":
            print(json.dumps(TelegramClient(settings).status(), indent=2))
            return 0

        if args.command == "telegram-webhook":
            client = TelegramClient(settings)
            if args.delete:
                print(json.dumps(client.delete_webhook(), indent=2))
                return 0
            if not settings.telegram_webhook_secret:
                raise ValueError("Set TELEGRAM_WEBHOOK_SECRET before registering the webhook.")
            url = args.base_url.rstrip("/") + "/api/telegram/webhook"
            print(json.dumps(client.set_webhook(url), indent=2))
            return 0

        if args.command == "agents-list":
            registry = PortfolioAgentRegistry(settings.portfolio_agent_dir)
            print(json.dumps(registry.list_agents(), indent=2))
            return 0

        if args.command == "agents-activate":
            registry = PortfolioAgentRegistry(settings.portfolio_agent_dir)
            print(json.dumps(registry.activate(args.version_id), indent=2))
            return 0

        if args.command == "agents-rollback":
            registry = PortfolioAgentRegistry(settings.portfolio_agent_dir)
            print(json.dumps(registry.rollback(), indent=2))
            return 0

        if args.command == "agents-register":
            registry = PortfolioAgentRegistry(settings.portfolio_agent_dir)
            try:
                metrics = json.loads(args.metrics_json)
            except json.JSONDecodeError as exc:
                raise ValueError("--metrics-json must be valid JSON.") from exc
            if not isinstance(metrics, dict):
                raise ValueError("--metrics-json must decode to a JSON object.")
            manifest = registry.register_sia_generation(
                version_id=args.version_id,
                source_target_agent=args.target_agent_path,
                metrics=metrics,
                label=args.label or args.version_id,
                source_run=args.source_run,
                status=args.status,
            )
            print(json.dumps(manifest.to_dict(), indent=2))
            return 0

        if args.command == "query":
            plan = (
                trader.build_search_plan(args.query)
                if args.interactive
                else trader.build_search_exec_plan(args.query)
            )
            if args.print_only:
                _print_plan(plan.command, plan.prompt)
                return 0
            if args.interactive:
                print(f"Launching interactive Codex query for {args.query}.")
                return trader.launch(plan)
            result = trader.run_query_capture(args.query)
            print_query_result(result)
            return result.returncode

        budget = _parse_budget(args.budget, settings)
        plan = trader.build_plan(args.mode, args.objective, budget)

        if args.command == "prompt":
            print(plan.prompt)
            return 0

        if args.print_only:
            _print_plan(plan.command, plan.prompt)
            return 0

        print(f"Launching Codex in {args.mode} mode.")
        return trader.launch(plan)
    except ValueError as exc:
        print(f"Error: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
