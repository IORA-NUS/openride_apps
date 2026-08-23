#!/usr/bin/env python
"""Migrate a legacy six-file scenario (dataset/<slug>/) to the self-contained
bundle layout (scenarios/<slug>/scenario.json).

Reads the old behavior files + orsim_settings (+ its embedded GENERATION_SPEC as the
recipe), compiles a single scenario.json, and writes it under the new scenarios/
folder. The old dataset/<slug>/ copy is left untouched (the runtime/back-compat
reader still handles it).

Usage (apps venv):
    PYTHONPATH=openride_apps python scripts/migrate_scenario_to_bundle.py \
        --slug multi_haulier_greedy_solver_500_trucks
"""

from __future__ import annotations

import argparse
import json
import os
import sys

# Make `apps` importable when run from the repo root.
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "openride_apps"))

from apps.container_logistics.scenario import scenario_bundle  # noqa: E402
from apps.simulation.container_logistics_wiring import get_datahub_dir, get_domain  # noqa: E402

_LEGACY = {
    "truck": "truck_behavior.json",
    "order": "order_behavior.json",
    "facility": "facility_behavior.json",
    "assignment": "assignment_behavior.json",
    "analytics": "analytics_behavior.json",
}


def _load_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as fp:
        return json.load(fp)


def migrate(datahub: str, domain: str, slug: str, *, overwrite: bool = False) -> str:
    from apps.container_logistics.scenario.frontend_scenario_spec import (
        container_logistics_scenarios_root,
    )

    src = os.path.join(datahub, domain, "dataset", slug)
    dst = os.path.join(container_logistics_scenarios_root(), slug)
    if not os.path.isdir(src):
        raise FileNotFoundError(f"Legacy scenario not found: {src}")
    if scenario_bundle.bundle_exists(dst) and not overwrite:
        raise FileExistsError(f"Bundle already exists at {dst} (use --overwrite)")

    collections = {role: _load_json(os.path.join(src, fname)) for role, fname in _LEGACY.items()}
    orsim_settings = _load_json(os.path.join(src, "orsim_settings.json"))
    recipe = orsim_settings.get("GENERATION_SPEC") if isinstance(orsim_settings, dict) else None

    meta_path = os.path.join(src, "scenario_meta.json")
    meta = _load_json(meta_path) if os.path.isfile(meta_path) else {}

    bundle = scenario_bundle.compile_bundle(
        collections,
        orsim_settings,
        recipe if isinstance(recipe, dict) else None,
        domain=domain,
        slug=slug,
        name=(recipe or {}).get("name") or meta.get("name") or slug,
        source=(recipe or {}).get("source") or meta.get("source") or "migrated",
        created_at=meta.get("createdAt"),
        behavior_revision=orsim_settings.get("BEHAVIOR_REVISION") if isinstance(orsim_settings, dict) else None,
    )
    path = scenario_bundle.write_bundle(dst, bundle)

    # Materialize the editable source spec.json from the recipe (so the migrated
    # scenario can be hand-edited + recompiled), then emit the browse-index.
    from apps.container_logistics.scenario import frontend_scenario_spec as fe  # noqa: E402
    from apps.container_logistics.scenario import scenario_index  # noqa: E402

    if isinstance(recipe, dict):
        fe.write_spec(dst, recipe)
    scenario_index.refresh_scenario(dst, slug, os.path.dirname(dst))
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--slug", required=True, help="Scenario folder name under dataset/")
    parser.add_argument("--domain", default=None)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()

    datahub = get_datahub_dir()
    domain = args.domain or get_domain()
    path = migrate(datahub, domain, args.slug, overwrite=args.overwrite)
    print(f"Wrote bundle: {path}")
    counts = scenario_bundle.read_bundle(os.path.dirname(path)).get("counts")
    print(f"Counts: {counts}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
