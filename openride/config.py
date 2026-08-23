"""Static configuration for the OpenRide headless CLI.

Mirrors the values the rest of the stack already uses (Mongo db/collections, control
command, domain) so the CLI reads exactly the same data the dashboard does.
"""

from __future__ import annotations

import os
from pathlib import Path

WORKSPACE_ROOT = Path(os.environ.get("OPENRIDE_WORKSPACE_ROOT", "/home/user"))

# The simulation + control code runs under the apps venv (has orsim/eventlet/pymongo/etc.).
APPS_PYTHON = Path(
    os.environ.get("OPENRIDE_PYTHON", str(WORKSPACE_ROOT / "openride_apps" / "venv" / "bin" / "python"))
)

# Mongo — same db/collections the analytics frontend reads (see analytics/lib/mongodb.ts
# and lib/kpiMetrics.ts / app/api/breakdown/route.ts).
MONGO_URI = os.environ.get("MONGODB_URI", "mongodb://127.0.0.1:27017")
MONGO_DB = os.environ.get("MONGODB_NAME", "OpenRoadDB")
BREAKDOWN_COLLECTION = "container_logistics_kpi_breakdown"
KPI_COLLECTION = "kpi"
RUN_CONFIG_COLLECTION = "run_config"

# apps/dataplane read API. Scalar KPIs come from here first: the legacy `apps/kpi_sink`
# that wrote the Mongo `kpi` collection is being retired, it exported only on a terminal
# `run_status` (so a killed run persisted nothing), and it was at-most-once by construction
# — 38 of 73 runs in `kpi` hold a truncated series as a result. The dataplane writes DuckDB
# continuously at ingest, so a run's scalars are readable the moment the flush lands, and
# the `kpi` collection stays as a read-only fallback for the historical runs it already
# holds. Same "dataplane first, Mongo on error OR empty" rule the dashboard follows
# (analytics/lib/dataplaneSource.ts).
DATAPLANE_URL = os.environ.get("DATAPLANE_URL", "http://127.0.0.1:8620")
DATAPLANE_TIMEOUT_S = float(os.environ.get("DATAPLANE_TIMEOUT_S", "2.0"))

DEFAULT_DOMAIN = os.environ.get("ORSIM_DOMAIN", "container_logistics")

# Per-run reports land next to the sim's own output.
REPORT_BASE = WORKSPACE_ROOT / "openride_apps" / "apps" / "output"

# Assignment solver strategies (mirror SOLVER_REGISTRY / the dashboard select).
SOLVERS = ["GreedyNearest", "RandomAssignment"]
DEFAULT_SOLVER = "GreedyNearest"

# Per-role generation policies (mirror the datagen POLICY_REGISTRY / `known-policies`
# control command / the dashboard dropdowns). `openride scenario new` validates a
# requested policy against these, and the server re-validates authoritatively.
POLICIES = {
    "truck": ["default", "random", "historical"],
    "order": ["historical", "random"],
    "facility": ["allocate", "random"],
}
DEFAULT_POLICIES = {"truck": "default", "order": "historical", "facility": "allocate"}

# How often the live dashboard re-polls Mongo for breakdown snapshots.
LIVE_POLL_SECONDS = float(os.environ.get("OPENRIDE_CLI_POLL_SECONDS", "2.0"))

# Minimum seconds between dashboard repaints. Higher = calmer/steadier (less flicker),
# lower = snappier. The view also only repaints when its content actually changes.
LIVE_PAINT_MIN_GAP_SECONDS = float(os.environ.get("OPENRIDE_CLI_PAINT_GAP_SECONDS", "0.5"))
