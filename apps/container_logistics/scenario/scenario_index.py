"""Derived browse-index cache for the dashboard scenario list/detail paths.

Two layers, both *derived* from ``scenario.json`` (never a source of truth — always
regenerable, so the "one file is enough to run" invariant holds):

- **Layer A** — per-scenario ``scenarios/<slug>/index.json``: the detail fields
  (list entry + ``preview`` + ``editForm`` + ``integrity``). The detail path reads this.
- **Layer B** — roll-up ``scenarios/_index.json``: one dict of *list-only* fields per
  scenario. The list path reads ONLY this file.

This exists because the run artifact ``scenario.json`` is fully inline (multi-MB), and
parsing every scenario's bundle to render the dashboard list is ~1.2 s for 17 scenarios.
The index lets the browse paths read a few KB instead. Freshness is verified by comparing
a recorded ``scenario.json`` mtime/size against ``os.stat`` (cheap; never a parse), so the
index self-heals against any write that bypassed the maintenance hooks.
"""

from __future__ import annotations

import json
import os
from typing import Any, Optional

INDEX_FILENAME = "index.json"
ROLLUP_FILENAME = "_index.json"
ROLLUP_VERSION = 1

# Internal freshness keys stripped before an entry is returned to the API.
_INTERNAL_KEYS = ("bundleMtime", "bundleSize")


# --------------------------------------------------------------------------- stat

def _bundle_stat(scenario_dir: str) -> tuple[Optional[int], Optional[int]]:
    from .scenario_bundle import bundle_path

    try:
        st = os.stat(bundle_path(scenario_dir))
        return int(st.st_mtime_ns), int(st.st_size)
    except OSError:
        return None, None


def is_fresh(entry: dict[str, Any], scenario_dir: str) -> bool:
    """True when ``entry``'s recorded mtime/size match the on-disk scenario.json."""
    mtime, size = _bundle_stat(scenario_dir)
    if mtime is None:
        return False
    return entry.get("bundleMtime") == mtime and entry.get("bundleSize") == size


def strip_internal(entry: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in entry.items() if k not in _INTERNAL_KEYS}


# ----------------------------------------------------------------- detail (Layer A)

def build_detail_entry(scenario_dir: str, slug: str) -> Optional[dict[str, Any]]:
    """Assemble the Layer-A detail entry for a bundle scenario, or None if the folder
    has no ``scenario.json`` (legacy / ridehail — caller falls back to direct readers).

    Reads ``scenario.json`` (a few times via the existing bundle-aware readers) — this
    only runs on a write/rebuild, never on the hot list path.
    """
    from .scenario_bundle import bundle_exists, read_bundle
    from . import frontend_scenario_spec as fe

    if not bundle_exists(scenario_dir):
        return None

    entry = fe.scenario_list_entry(scenario_dir, slug)
    generation_spec = fe._load_generation_spec_from_disk(scenario_dir)
    edit_form = fe.scenario_edit_form_from_detail(entry, generation_spec, scenario_path=scenario_dir)

    bundle = read_bundle(scenario_dir) or {}
    preview = bundle.get("preview") if isinstance(bundle.get("preview"), dict) else {}
    integrity = bundle.get("integrity") if isinstance(bundle.get("integrity"), dict) else {}
    mtime, size = _bundle_stat(scenario_dir)

    return {
        "entry": entry,
        "preview": preview,
        "editForm": edit_form,
        "integrity": integrity,
        # extra chips surfaced on the list (additive — frontend ignores unknown fields)
        "solver": edit_form.get("solver"),
        "hauliers": edit_form.get("hauliers"),
        "bundleMtime": mtime,
        "bundleSize": size,
    }


def list_fields(detail_entry: dict[str, Any]) -> dict[str, Any]:
    """Project the Layer-B (list-only) subset from a detail entry."""
    entry = detail_entry.get("entry") if isinstance(detail_entry.get("entry"), dict) else {}
    return {
        **entry,
        "solver": detail_entry.get("solver"),
        "hauliers": detail_entry.get("hauliers"),
        "bundleMtime": detail_entry.get("bundleMtime"),
        "bundleSize": detail_entry.get("bundleSize"),
    }


def index_path(scenario_dir: str) -> str:
    return os.path.join(scenario_dir, INDEX_FILENAME)


def read_index(scenario_dir: str) -> Optional[dict[str, Any]]:
    try:
        with open(index_path(scenario_dir), "r", encoding="utf-8") as fp:
            data = json.load(fp)
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def write_index(scenario_dir: str, detail_entry: dict[str, Any]) -> None:
    os.makedirs(scenario_dir, exist_ok=True)
    target = index_path(scenario_dir)
    tmp = target + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fp:
        json.dump(detail_entry, fp, indent=2)
        fp.write("\n")
    os.replace(tmp, target)


# ------------------------------------------------------------------ rollup (Layer B)

def _rollup_path(scenarios_root: str) -> str:
    return os.path.join(scenarios_root, ROLLUP_FILENAME)


def read_rollup(scenarios_root: str) -> dict[str, Any]:
    """Return ``{slug: list_entry}`` (the rollup's entries), or {} if absent/corrupt."""
    try:
        with open(_rollup_path(scenarios_root), "r", encoding="utf-8") as fp:
            data = json.load(fp)
    except (OSError, json.JSONDecodeError):
        return {}
    entries = data.get("entries") if isinstance(data, dict) else None
    return entries if isinstance(entries, dict) else {}


def _write_rollup(scenarios_root: str, entries: dict[str, Any]) -> None:
    os.makedirs(scenarios_root, exist_ok=True)
    target = _rollup_path(scenarios_root)
    tmp = target + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fp:
        json.dump({"version": ROLLUP_VERSION, "entries": entries}, fp, indent=2)
        fp.write("\n")
    os.replace(tmp, target)


def upsert_rollup(scenarios_root: str, slug: str, list_entry: dict[str, Any]) -> None:
    entries = read_rollup(scenarios_root)
    entries[slug] = list_entry
    _write_rollup(scenarios_root, entries)


def remove_from_rollup(scenarios_root: str, slug: str) -> None:
    entries = read_rollup(scenarios_root)
    if slug in entries:
        del entries[slug]
        _write_rollup(scenarios_root, entries)


# ---------------------------------------------------------------- high-level helpers

def refresh_scenario(scenario_dir: str, slug: str, scenarios_root: str) -> Optional[dict[str, Any]]:
    """(Re)build ``index.json`` + upsert the rollup for one bundle scenario.

    Returns the detail entry, or None when the folder has no bundle (caller falls back).
    """
    detail = build_detail_entry(scenario_dir, slug)
    if detail is None:
        return None
    write_index(scenario_dir, detail)
    upsert_rollup(scenarios_root, slug, list_fields(detail))
    return detail


def rebuild_rollup(datahub_dir: str, domain: str) -> dict[str, Any]:
    """Scan every scenario folder and rebuild ``_index.json`` (+ each ``index.json``).

    The drift / first-run escape hatch. Skips legacy folders without a bundle.
    """
    from .frontend_scenario_spec import scenario_root

    root = scenario_root(datahub_dir, domain)
    entries: dict[str, Any] = {}
    if os.path.isdir(root):
        for name in sorted(os.listdir(root)):
            if name.startswith(".") or name.endswith(".tmp") or name == ROLLUP_FILENAME:
                continue
            sdir = os.path.join(root, name)
            if not os.path.isdir(sdir):
                continue
            detail = build_detail_entry(sdir, name)
            if detail is None:
                continue
            write_index(sdir, detail)
            entries[name] = list_fields(detail)
    _write_rollup(root, entries)
    return {"ok": True, "scenarios": len(entries)}
