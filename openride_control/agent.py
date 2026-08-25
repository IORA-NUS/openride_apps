"""Kafka control agent: consumes service_control, runs ServiceManager, publishes status."""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import threading
import time
import uuid

from openride_control.kafka_bus import (
    consume_control_commands,
    ensure_control_topics,
    publish_all_status,
    publish_event,
    publish_status,
    wait_for_broker,
)
from openride_control.manager import ServiceManager
from openride_control.models import ControlCommand, ControlEvent
from openride_control.registry import SERVICES

log = logging.getLogger(__name__)


class ControlAgent:
    def __init__(self, *, status_interval_s: float = 2.0) -> None:
        self.mgr = ServiceManager()
        self.status_interval_s = status_interval_s
        self._stop = threading.Event()

    def publish_snapshots(self) -> None:
        publish_all_status([s.to_dict() for s in self.mgr.all_snapshots()])

    def handle_command(self, data: dict) -> None:
        cmd = ControlCommand.from_dict(data)
        if not cmd.command_id:
            cmd.command_id = str(uuid.uuid4())

        log.info("Command %s action=%s service=%s", cmd.command_id, cmd.action, cmd.service)

        try:
            if cmd.action == "start":
                if not cmd.service or cmd.service not in SERVICES:
                    raise ValueError(f"Unknown service: {cmd.service!r}")
                result = self.mgr.start(cmd.service, with_dependencies=cmd.with_dependencies)
            elif cmd.action == "stop":
                if not cmd.service or cmd.service not in SERVICES:
                    raise ValueError(f"Unknown service: {cmd.service!r}")
                result = self.mgr.stop(cmd.service, force=cmd.force)
            elif cmd.action == "start_backend":
                results = self.mgr.start_backend()
                result = results[-1] if results else None
                ok = all(r.ok for r in results)
                event = ControlEvent(
                    command_id=cmd.command_id,
                    ok=ok,
                    action=cmd.action,
                    service=None,
                    message="Backend start finished" if ok else "Some services failed to start",
                )
                publish_event(event.to_dict())
                self.publish_snapshots()
                return
            elif cmd.action == "stop_all":
                results = self.mgr.stop_all(force=cmd.force)
                ok = all(r.ok for r in results)
                event = ControlEvent(
                    command_id=cmd.command_id,
                    ok=ok,
                    action=cmd.action,
                    service=None,
                    message="Stop all finished" if ok else "Some services failed to stop",
                )
                publish_event(event.to_dict())
                self.publish_snapshots()
                return
            elif cmd.action == "run_simulation":
                scenario = None
                if isinstance(data.get("scenario"), str) and data["scenario"].strip():
                    scenario = data["scenario"].strip()
                run_name = None
                if isinstance(data.get("runName"), str) and data["runName"].strip():
                    run_name = data["runName"].strip()
                elif isinstance(data.get("run_name"), str) and data["run_name"].strip():
                    run_name = data["run_name"].strip()
                result = self.mgr.run_simulation_background(scenario=scenario, run_name=run_name)
            elif cmd.action == "stop_simulation":
                result = self.mgr.stop_simulation_command()
            else:
                raise ValueError(f"Unknown action: {cmd.action!r}")

            if result is None:
                return

            event = ControlEvent(
                command_id=cmd.command_id,
                ok=result.ok,
                action=cmd.action,
                service=cmd.service or result.key,
                message=result.message,
                blocked_by=result.blocked_by,
            )
            publish_event(event.to_dict())
            service_key = cmd.service or result.key
            if service_key in SERVICES:
                publish_status(self.mgr.snapshot(service_key).to_dict())
            self.publish_snapshots()
        except Exception as ex:
            log.exception("Command failed")
            publish_event(
                ControlEvent(
                    command_id=cmd.command_id,
                    ok=False,
                    action=cmd.action,
                    service=cmd.service,
                    message=str(ex),
                ).to_dict()
            )

    def _status_loop(self) -> None:
        while not self._stop.wait(self.status_interval_s):
            try:
                self.publish_snapshots()
            except Exception:
                log.exception("Status heartbeat failed")

    def run(self) -> None:
        if not wait_for_broker():
            log.error(
                "Kafka broker not reachable. Start Kafka first (e.g. python start.py start kafka)."
            )
            sys.exit(1)

        ensure_control_topics()
        self.publish_snapshots()

        status_thread = threading.Thread(target=self._status_loop, daemon=True)
        status_thread.start()

        def _on_signal(*_args) -> None:
            log.info("Shutting down control agent")
            self._stop.set()
            sys.exit(0)

        signal.signal(signal.SIGINT, _on_signal)
        signal.signal(signal.SIGTERM, _on_signal)

        consume_control_commands(self.handle_command)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="OpenRide Kafka service control agent")
    parser.add_argument(
        "--status-interval",
        type=float,
        default=2.0,
        help="Seconds between status heartbeats (default: 2)",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    ControlAgent(status_interval_s=args.status_interval).run()


if __name__ == "__main__":
    main()
