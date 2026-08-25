"""Shared types for service orchestration."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ServiceState(str, Enum):
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    FAILED = "failed"
    UNKNOWN = "unknown"


@dataclass
class ActionResult:
    key: str
    ok: bool
    state: ServiceState
    message: str = ""
    blocked_by: list[str] | None = None


@dataclass
class ServiceSnapshot:
    key: str
    label: str
    description: str
    kind: str
    state: ServiceState
    dependencies: tuple[str, ...] = ()
    dependents: tuple[str, ...] = ()
    message: str = ""
    # Sub-state for services that can be *up but not well* (today: the dataplane,
    # whose /health reports per-task liveness). Kept separate from `state` on
    # purpose: a degraded service is still running, so start/stop gating — which
    # keys off `state` everywhere — must not change. "ok" | "degraded".
    health: str = "ok"

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "description": self.description,
            "kind": self.kind,
            "state": self.state.value,
            "dependencies": list(self.dependencies),
            "dependents": list(self.dependents),
            "message": self.message,
            "health": self.health,
        }


@dataclass
class ControlCommand:
    command_id: str
    action: str
    service: str | None = None
    with_dependencies: bool = True
    force: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ControlCommand:
        return cls(
            command_id=str(data.get("commandId") or data.get("command_id") or ""),
            action=str(data.get("action") or "").strip().lower(),
            service=(str(data["service"]).strip() if data.get("service") else None),
            with_dependencies=bool(data.get("withDependencies", data.get("with_dependencies", True))),
            force=bool(data.get("force", False)),
        )


@dataclass
class ControlEvent:
    """Command outcome published to service_events."""

    command_id: str
    ok: bool
    action: str
    service: str | None
    message: str = ""
    blocked_by: list[str] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "commandId": self.command_id,
            "ok": self.ok,
            "action": self.action,
            "service": self.service,
            "message": self.message,
            "blockedBy": self.blocked_by or [],
        }
