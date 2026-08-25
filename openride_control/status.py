"""Emit live service status as JSON (docker compose + process probes)."""

from __future__ import annotations

import json
import sys

from openride_control.manager import ServiceManager


def collect_status_json() -> dict:
    mgr = ServiceManager()
    return {"services": [s.to_dict() for s in mgr.all_snapshots()]}


def main() -> None:
    json.dump(collect_status_json(), sys.stdout)


if __name__ == "__main__":
    main()
