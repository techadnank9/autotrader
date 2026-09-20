from __future__ import annotations

if __package__ in {None, ""}:
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import shutil
import subprocess
from dataclasses import dataclass
from decimal import Decimal
import json
import sys
from typing import Any

from ai_trader.config import Settings
from ai_trader.prompts import (
    direct_search_prompt,
    launcher_note,
    live_prompt,
    portfolio_execution_prompt,
    portfolio_snapshot_prompt,
    preview_prompt,
    recommendation_prompt,
)

SEARCH_SERVER_NAME = "robinhood-trading"


@dataclass
class LaunchPlan:
    mode: str
    prompt: str
    command: list[str]


@dataclass
class QueryRunResult:
    returncode: int
    final_text: str | None
    stdout: str
    stderr: str


class RobinhoodTrader:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def build_plan(self, mode: str, objective: str, budget: Decimal) -> LaunchPlan:
        prompt = self._prompt_for_mode(mode, objective, budget)
        command = self._build_interactive_codex_command(prompt, enable_web_search=True)
        return LaunchPlan(mode=mode, prompt=prompt, command=command)

    def build_search_plan(self, query: str) -> LaunchPlan:
        prompt = direct_search_prompt(query)
        command = self._build_interactive_codex_command(prompt, enable_web_search=False)
        return LaunchPlan(mode="query", prompt=prompt, command=command)

    def build_search_exec_plan(self, query: str) -> LaunchPlan:
        prompt = direct_search_prompt(query)
        command = self._build_exec_codex_command(prompt)
        return LaunchPlan(mode="query-exec", prompt=prompt, command=command)

    def launch(self, plan: LaunchPlan) -> int:
        try:
            result = subprocess.run(plan.command, check=False)
        except OSError as exc:
            print(f"Could not launch Codex: {exc}")
            return 1
        return result.returncode

    def run_query_capture(self, query: str) -> QueryRunResult:
        plan = self.build_search_exec_plan(query)
        try:
            result = subprocess.run(
                plan.command,
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError as exc:
            return QueryRunResult(returncode=1, final_text=None, stdout="", stderr=str(exc))
        final_text = self._extract_final_agent_text(result.stdout)
        return QueryRunResult(
            returncode=result.returncode,
            final_text=final_text,
            stdout=result.stdout,
            stderr=result.stderr,
        )

    def run_json_prompt(self, prompt: str, *, timeout: int = 240) -> dict[str, Any]:
        try:
            result = subprocess.run(
                self._build_exec_codex_command(prompt),
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout,
            )
        except FileNotFoundError:
            return {
                "status": "failed",
                "message": f"Codex binary {self.settings.codex_bin!r} was not found on PATH.",
                "stderr": "",
                "stdout": "",
                "returncode": -1,
            }
        except subprocess.TimeoutExpired:
            return {
                "status": "failed",
                "message": f"Codex did not respond within {timeout}s.",
                "stderr": "",
                "stdout": "",
                "returncode": -1,
            }
        final_text = self._extract_final_agent_text(result.stdout)
        if result.returncode == 0 and final_text:
            try:
                return json.loads(final_text)
            except json.JSONDecodeError:
                return {"status": "failed", "message": "Unparseable JSON response.", "raw": final_text}
        return {
            "status": "failed",
            "message": "Codex did not return a usable final JSON response.",
            "stderr": result.stderr[-1200:],
            "stdout": result.stdout[-1200:],
            "returncode": result.returncode,
        }

    def fetch_portfolio_snapshot(self, symbols: list[str]) -> dict[str, Any]:
        prompt = portfolio_snapshot_prompt(symbols)
        snapshot = self.run_json_prompt(prompt, timeout=240)
        if snapshot.get("status") == "failed":
            return {
                "agentic_account": {"account_id": None, "account_number_masked": None},
                "portfolio": {"total_value": 0, "buying_power": 0, "cash_available": 0},
                "positions": [],
                "quotes": {},
                "tradability": {},
                "recent_orders": [],
                "warnings": [snapshot.get("message", "Snapshot capture failed.")],
                "raw_error": snapshot,
            }
        return snapshot

    def execute_management_plan(self, plan: dict[str, Any]) -> dict[str, Any]:
        actions = plan.get("ordered_actions") or plan.get("actions") or []
        if not actions:
            return {
                "status": "no_op",
                "summary": "No actionable portfolio changes were generated.",
                "reviewed_orders": [],
                "placed_orders": [],
                "skipped_orders": [],
                "warnings": plan.get("warnings", []),
            }
        return self.run_json_prompt(portfolio_execution_prompt(plan), timeout=360)

    def doctor(self) -> dict[str, Any]:
        codex_path = shutil.which(self.settings.codex_bin)
        mcp_status = self._mcp_status() if codex_path else {"configured": False, "status_line": None}
        return {
            "codex_bin": self.settings.codex_bin,
            "codex_found": bool(codex_path),
            "codex_path": codex_path,
            "robinhood_mcp_url": self.settings.robinhood_mcp_url,
            "web_search_enabled": self.settings.enable_web_search,
            "robinhood_mcp_configured": mcp_status["configured"],
            "robinhood_mcp_status_line": mcp_status["status_line"],
            "launcher_note": launcher_note(),
        }

    def _mcp_status(self) -> dict[str, Any]:
        try:
            result = subprocess.run(
                [self.settings.codex_bin, "mcp", "list"],
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"configured": False, "status_line": str(exc)}
        if result.returncode != 0:
            return {
                "configured": False,
                "status_line": result.stderr.strip() or result.stdout.strip() or None,
            }
        for line in result.stdout.splitlines():
            if line.strip().startswith(SEARCH_SERVER_NAME):
                return {"configured": True, "status_line": line.rstrip()}
        return {"configured": False, "status_line": None}

    def _prompt_for_mode(self, mode: str, objective: str, budget: Decimal) -> str:
        if mode == "recommend":
            return recommendation_prompt(objective, budget)
        if mode == "preview":
            return preview_prompt(objective, budget)
        if mode == "live":
            return live_prompt(objective, budget)
        raise ValueError(f"Unsupported mode: {mode}")

    def _extract_final_agent_text(self, stdout: str) -> str | None:
        last_text: str | None = None
        for line in stdout.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            try:
                event = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if event.get("type") != "item.completed":
                continue
            item = event.get("item", {})
            if item.get("type") == "agent_message" and isinstance(item.get("text"), str):
                last_text = item["text"]
        return last_text

    def _build_interactive_codex_command(
        self,
        prompt: str,
        *,
        enable_web_search: bool,
    ) -> list[str]:
        command = [self.settings.codex_bin]
        if enable_web_search and self.settings.enable_web_search:
            command.append("--search")
        command.append(prompt)
        return command

    def _build_exec_codex_command(self, prompt: str) -> list[str]:
        return [
            self.settings.codex_bin,
            "exec",
            "--skip-git-repo-check",
            "--json",
            prompt,
        ]


def print_query_result(result: QueryRunResult) -> None:
    if result.final_text:
        print(result.final_text)
    elif result.stdout:
        print(result.stdout)
    if result.returncode != 0 and result.stderr:
        print(result.stderr)


def main() -> int:
    settings = Settings.load()
    trader = RobinhoodTrader(settings)
    query = sys.argv[1] if len(sys.argv) > 1 else "AAPL"
    result = trader.run_query_capture(query)
    print_query_result(result)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
