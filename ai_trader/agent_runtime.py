"""Registry of portfolio-manager versions, with activation and rollback.

The built-in manager (``ai_trader.portfolio.plan_portfolio``) is always
available. SIA-generated managers are registered as files on disk and are only
promoted into production by an explicit ``activate`` call.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from ai_trader.portfolio import BUILTIN_VERSION_ID, PortfolioPolicy, plan_portfolio

Planner = Callable[[dict[str, Any], list[dict[str, Any]], PortfolioPolicy], dict[str, Any]]

STATE_FILE = "state.json"
MANIFEST_DIR = "manifests"
CODE_DIR = "code"


@dataclass
class AgentManifest:
    version_id: str
    label: str
    source_run: str = "builtin"
    status: str = "passed"
    kind: str = "sia"
    module_path: str | None = None
    metrics: dict[str, Any] = field(default_factory=dict)
    registered_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "version_id": self.version_id,
            "label": self.label,
            "source_run": self.source_run,
            "status": self.status,
            "kind": self.kind,
            "module_path": self.module_path,
            "metrics": self.metrics,
            "registered_at": self.registered_at,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "AgentManifest":
        return cls(
            version_id=str(payload.get("version_id") or ""),
            label=str(payload.get("label") or payload.get("version_id") or ""),
            source_run=str(payload.get("source_run") or "unknown"),
            status=str(payload.get("status") or "pending"),
            kind=str(payload.get("kind") or "sia"),
            module_path=payload.get("module_path"),
            metrics=payload.get("metrics") if isinstance(payload.get("metrics"), dict) else {},
            registered_at=str(payload.get("registered_at") or ""),
        )


BUILTIN_MANIFEST = AgentManifest(
    version_id=BUILTIN_VERSION_ID,
    label="Built-in conservative manager",
    source_run="builtin",
    status="passed",
    kind="builtin",
    module_path=None,
    metrics={},
    registered_at="",
)


class PortfolioAgentRegistry:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.manifest_dir = self.root / MANIFEST_DIR
        self.code_dir = self.root / CODE_DIR
        self.state_path = self.root / STATE_FILE

    # -- state ---------------------------------------------------------------

    def _read_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {"current": BUILTIN_VERSION_ID, "previous": None}
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {"current": BUILTIN_VERSION_ID, "previous": None}
        if not isinstance(state, dict):
            return {"current": BUILTIN_VERSION_ID, "previous": None}
        state.setdefault("current", BUILTIN_VERSION_ID)
        state.setdefault("previous", None)
        return state

    def _write_state(self, state: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")

    # -- manifests -----------------------------------------------------------

    def _manifests(self) -> dict[str, AgentManifest]:
        manifests: dict[str, AgentManifest] = {BUILTIN_VERSION_ID: BUILTIN_MANIFEST}
        if not self.manifest_dir.exists():
            return manifests
        for path in sorted(self.manifest_dir.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            manifest = AgentManifest.from_dict(payload)
            if manifest.version_id:
                manifests[manifest.version_id] = manifest
        return manifests

    def list_agents(self) -> dict[str, Any]:
        state = self._read_state()
        manifests = self._manifests()
        current = state.get("current") or BUILTIN_VERSION_ID
        if current not in manifests:
            current = BUILTIN_VERSION_ID
        previous = state.get("previous")
        if previous not in manifests:
            previous = None
        eligible = [
            manifest.to_dict()
            for manifest in manifests.values()
            if manifest.status == "passed" or manifest.version_id == current
        ]
        eligible.sort(key=lambda item: (item["version_id"] != BUILTIN_VERSION_ID, item["version_id"]))
        return {
            "current": current,
            "previous": previous,
            "eligible": eligible,
            "all": [manifest.to_dict() for manifest in manifests.values()],
        }

    def activate(self, version_id: str) -> dict[str, Any]:
        manifests = self._manifests()
        manifest = manifests.get(version_id)
        if manifest is None:
            raise ValueError(f"Unknown portfolio-agent version: {version_id}")
        if manifest.status != "passed":
            raise ValueError(f"Version {version_id} has status {manifest.status!r} and cannot be activated.")
        if manifest.kind != "builtin":
            module_path = Path(manifest.module_path or "")
            if not module_path.is_file():
                raise ValueError(f"Version {version_id} is registered but its module is missing: {module_path}")

        state = self._read_state()
        current = state.get("current") or BUILTIN_VERSION_ID
        if current != version_id:
            state["previous"] = current
        state["current"] = version_id
        state["activated_at"] = datetime.now(timezone.utc).isoformat()
        self._write_state(state)
        return self.list_agents()

    def rollback(self) -> dict[str, Any]:
        state = self._read_state()
        previous = state.get("previous")
        if not previous:
            raise ValueError("No previous portfolio-agent version to roll back to.")
        return self.activate(previous)

    def register_sia_generation(
        self,
        *,
        version_id: str,
        source_target_agent: str,
        metrics: dict[str, Any],
        label: str | None = None,
        source_run: str = "manual",
        status: str = "passed",
    ) -> AgentManifest:
        source = Path(source_target_agent)
        if not source.is_file():
            raise ValueError(f"Target agent file not found: {source}")

        self.code_dir.mkdir(parents=True, exist_ok=True)
        self.manifest_dir.mkdir(parents=True, exist_ok=True)
        destination = self.code_dir / f"{version_id}.py"
        shutil.copyfile(source, destination)

        manifest = AgentManifest(
            version_id=version_id,
            label=label or version_id,
            source_run=source_run,
            status=status,
            kind="sia",
            module_path=str(destination.resolve()),
            metrics=metrics,
            registered_at=datetime.now(timezone.utc).isoformat(),
        )
        (self.manifest_dir / f"{version_id}.json").write_text(
            json.dumps(manifest.to_dict(), indent=2), encoding="utf-8"
        )
        return manifest

    # -- loading -------------------------------------------------------------

    def load_active_planner(self) -> tuple[Planner, dict[str, Any]]:
        """Return the active planner plus a note describing how it resolved."""
        state = self._read_state()
        version_id = state.get("current") or BUILTIN_VERSION_ID
        manifests = self._manifests()
        manifest = manifests.get(version_id, BUILTIN_MANIFEST)

        if manifest.kind == "builtin" or not manifest.module_path:
            return plan_portfolio, {"version_id": BUILTIN_VERSION_ID, "source": "builtin"}

        try:
            planner = _load_planner_from_path(Path(manifest.module_path), manifest.version_id)
        except Exception as exc:
            return plan_portfolio, {
                "version_id": BUILTIN_VERSION_ID,
                "source": "builtin_fallback",
                "requested": manifest.version_id,
                "error": str(exc),
            }
        return planner, {"version_id": manifest.version_id, "source": "sia", "module_path": manifest.module_path}


def _load_planner_from_path(path: Path, version_id: str) -> Planner:
    spec = importlib.util.spec_from_file_location(f"ai_trader_agent_{version_id}", path)
    if spec is None or spec.loader is None:
        raise ValueError(f"Cannot import portfolio agent from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    planner = getattr(module, "plan_portfolio", None)
    if not callable(planner):
        raise ValueError(f"{path} does not define a callable plan_portfolio(snapshot, ranking, policy).")
    return planner
