"""SIA (self-improving agent) integration for the portfolio-management task.

SIA is optional. When the binary is missing the UI reports that explicitly and
disables the run button rather than failing a request.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ai_trader.config import Settings
from ai_trader.market_research import MarketResearchService
from ai_trader.replay import ReplayStore, YahooEODPriceProvider


class SIAService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.replay = ReplayStore(settings.replay_log_dir)
        self.market = MarketResearchService(settings, self.replay)

    # -- status --------------------------------------------------------------

    def binary_path(self) -> str | None:
        candidate = Path(self.settings.sia_bin)
        if candidate.is_file():
            return str(candidate.resolve())
        return shutil.which(self.settings.sia_bin)

    def status(self) -> dict[str, Any]:
        binary = self.binary_path()
        task_dir = Path(self.settings.sia_task_dir)
        data_file = task_dir / "data" / "replay.jsonl"
        labels = self.replay.load_labels()
        runs = self.replay.list_runs()
        return {
            "installed": bool(binary),
            "binary": binary or self.settings.sia_bin,
            "task_dir": str(task_dir),
            "task_ready": data_file.is_file(),
            "case_count": _line_count(data_file),
            "meta_profile": self.settings.sia_meta_profile,
            "target_profile": self.settings.sia_target_profile,
            "web_port": self.settings.sia_web_port,
            "logged_runs": len(runs),
            "labeled_runs": len(labels),
            "pending_labels": max(len(runs) - len(labels), 0),
            "message": (
                "SIA is available on PATH."
                if binary
                else f"`{self.settings.sia_bin}` was not found on PATH; install it to enable SIA runs."
            ),
        }

    # -- dataset -------------------------------------------------------------

    def build_replay_dataset(self) -> dict[str, Any]:
        snapshot = self.market.collect_daily_snapshot()
        label_result = self.replay.build_labels(YahooEODPriceProvider())
        market_labels = self.market.build_labels()
        exported = self.replay.export_sia_replay_dataset(self.settings.sia_task_dir)
        self._ensure_task_scaffold(Path(self.settings.sia_task_dir))
        return {
            **exported,
            "labels_built": label_result.get("labels_built", 0),
            "pending_labels": label_result.get("pending_labels", exported.get("pending_labels", 0)),
            "market_snapshot_id": snapshot.get("snapshot_id"),
            "market_labels": len(market_labels),
            "built_at": datetime.now(timezone.utc).isoformat(),
        }

    def _ensure_task_scaffold(self, task_dir: Path) -> None:
        """Write a runnable baseline target agent if the task has none."""
        target = task_dir / "target_agent.py"
        if target.exists():
            return
        task_dir.mkdir(parents=True, exist_ok=True)
        target.write_text(
            '"""Baseline target agent for the portfolio-management replay task.\n\n'
            'SIA mutates this file. It must keep the same entrypoint signature.\n'
            '"""\n\n'
            "from __future__ import annotations\n\n"
            "import sys\n"
            "from pathlib import Path\n\n"
            "sys.path.insert(0, str(Path(__file__).resolve().parents[2]))\n\n"
            "from ai_trader.portfolio import PortfolioPolicy, plan_portfolio\n\n\n"
            "def solve(case: dict) -> dict:\n"
            '    """Return the management plan for one replay case."""\n'
            '    payload = case.get("input", {})\n'
            "    return plan_portfolio(\n"
            '        payload.get("account_snapshot", {}),\n'
            '        payload.get("ranking", []),\n'
            "        PortfolioPolicy(),\n"
            "    )\n",
            encoding="utf-8",
        )

    # -- run -----------------------------------------------------------------

    def run(self, *, max_generations: int = 1, build_replay_first: bool = True) -> dict[str, Any]:
        binary = self.binary_path()
        if not binary:
            raise RuntimeError(
                f"`{self.settings.sia_bin}` is not installed or not on PATH, so a SIA run cannot start."
            )

        build_result: dict[str, Any] = {}
        if build_replay_first:
            build_result = self.build_replay_dataset()

        command = [
            binary,
            "run",
            "--task",
            self.settings.sia_task_dir,
            "--max-generations",
            str(max_generations),
        ]
        for flag, value in (
            ("--meta-profile", self.settings.sia_meta_profile),
            ("--target-profile", self.settings.sia_target_profile),
        ):
            if Path(value).is_file():
                command.extend([flag, value])

        try:
            completed = subprocess.run(command, capture_output=True, text=True, check=False, timeout=3600)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {
                "status": "failed",
                "command": command,
                "returncode": -1,
                "stdout": "",
                "stderr": str(exc),
                "build": build_result,
                "summary": self._summary({}, build_result, status="failed", error=str(exc)),
            }

        status = "completed" if completed.returncode == 0 else "failed"
        parsed = _parse_run_output(completed.stdout)
        return {
            "status": status,
            "command": command,
            "returncode": completed.returncode,
            "stdout": completed.stdout[-8000:],
            "stderr": completed.stderr[-4000:],
            "build": build_result,
            "summary": self._summary(parsed, build_result, status=status),
        }

    def _summary(
        self,
        parsed: dict[str, Any],
        build_result: dict[str, Any],
        *,
        status: str,
        error: str | None = None,
    ) -> dict[str, Any]:
        metrics = parsed.get("metrics") if isinstance(parsed.get("metrics"), dict) else {}
        return {
            "status": status,
            "headline": parsed.get("headline")
            or ("SIA run finished" if status == "completed" else "SIA run did not finish"),
            "version_id": parsed.get("version_id"),
            "comparison_run_id": parsed.get("comparison_run_id"),
            "metrics": {
                "score": metrics.get("score", 0),
                "valid_output_rate": metrics.get("valid_output_rate", 0),
                "scored_count": metrics.get("scored_count", build_result.get("cases", 0)),
                "sample_count": metrics.get("sample_count", build_result.get("cases", 0)),
                "pending_labels": build_result.get("pending_labels", 0),
            },
            "mistakes": parsed.get("mistakes") or ([error] if error else []),
            "improvements": parsed.get("improvements") or [],
        }

    # -- traces --------------------------------------------------------------

    def reasoning_trace(self, kind: str, result: dict[str, Any]) -> list[dict[str, str]]:
        if kind == "build":
            return [
                {
                    "stage": "collect",
                    "title": "Market snapshot",
                    "detail": f"Snapshot {result.get('market_snapshot_id', '--')} collected for the replay universe.",
                },
                {
                    "stage": "label",
                    "title": "Forward labels",
                    "detail": (
                        f"{result.get('labels_built', 0)} run label(s) built, "
                        f"{result.get('pending_labels', 0)} still waiting on next-session prices."
                    ),
                },
                {
                    "stage": "export",
                    "title": "Replay dataset",
                    "detail": f"{result.get('cases', 0)} case(s) exported to {result.get('task_dir', '--')}.",
                },
            ]

        summary = result.get("summary") or {}
        metrics = summary.get("metrics") or {}
        return [
            {
                "stage": "prepare",
                "title": "Dataset",
                "detail": f"{(result.get('build') or {}).get('cases', 0)} replay case(s) available for scoring.",
            },
            {
                "stage": "run",
                "title": "SIA execution",
                "detail": f"Command exited {result.get('returncode', '--')} with status {result.get('status', 'unknown')}.",
            },
            {
                "stage": "score",
                "title": "Evaluation",
                "detail": (
                    f"Score {metrics.get('score', 0)} over "
                    f"{metrics.get('scored_count', 0)}/{metrics.get('sample_count', 0)} case(s)."
                ),
            },
            {
                "stage": "promote",
                "title": "Promotion",
                "detail": (
                    f"Candidate {summary.get('version_id')} is registered but not auto-promoted; activate it explicitly."
                    if summary.get("version_id")
                    else "No new candidate version was registered by this run."
                ),
            },
        ]


def _parse_run_output(stdout: str) -> dict[str, Any]:
    """Pick up the last JSON object SIA emitted, if it emitted one."""
    for line in reversed(stdout.splitlines()):
        stripped = line.strip()
        if not stripped.startswith("{"):
            continue
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    return {}


def _line_count(path: Path) -> int:
    if not path.is_file():
        return 0
    try:
        with path.open("r", encoding="utf-8") as handle:
            return sum(1 for line in handle if line.strip())
    except OSError:
        return 0
