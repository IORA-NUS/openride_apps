"""Single read/write authority for the compiled ``scenario.json`` bundle.

A scenario is now a self-contained folder whose authoritative artifact is one
``scenario.json`` — settings + all five agent collections + the generation recipe
+ a small header (counts/preview/integrity), all inline. Everything that runs or
inspects a scenario reads this one file.

Design notes
------------
- **Fully inline, no shards.** Every collection lives in ``agents.<role>`` inside
  the one file. The runtime parses it once at load (never in the step loop), so a
  ~9 MB file is a ~75 ms one-off; the dashboard list path (which parses every
  scenario) is a known, separately-deferred cost — see the plan's Stress-test §1.
- **Header first.** ``counts`` / ``preview`` / ``integrity`` are emitted before the
  heavy ``agents`` blob so a future list-path optimization can do a bounded,
  head-only read without materializing all agents.
- The *truly required to run* sections are ``settings`` + ``agents``. ``recipe`` is
  for edit/regenerate; ``preview`` / ``counts`` are for the dashboard list/detail.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Optional

BUNDLE_FILENAME = "scenario.json"
BUNDLE_SCHEMA_VERSION = 1

# The five original agent collections, in a stable order. Kept as-is (do NOT append
# to this tuple) because several existing call sites/tests destructure
# `load_bundle()`'s primary `collections` dict expecting exactly this key set
# (e.g. tests/test_scenario_compile.py::test_run_artifact_still_only_scenario_json).
COLLECTION_KEYS = ("truck", "order", "facility", "assignment", "analytics")

# Additional per-run service-agent collections (added after the original five —
# currently just `order_lifecycle`, WP6). Persisted into `agents` alongside the core
# five, but intentionally kept OUT of `COLLECTION_KEYS` / `load_bundle()`'s primary
# `collections` dict so that shape stays exactly as before; fetch these via
# `load_bundle_collection()` instead. A bundle predating a given optional key simply
# has no such entry under `agents` — `load_bundle_collection` returns `{}` for it.
OPTIONAL_COLLECTION_KEYS = ("order_lifecycle",)

# How many sample agents to embed in the header preview block.
_PREVIEW_TRUCK = 20
_PREVIEW_ORDER = 10


def bundle_path(scenario_dir: str) -> str:
    return os.path.join(scenario_dir, BUNDLE_FILENAME)


def bundle_exists(scenario_dir: str) -> bool:
    return os.path.isfile(bundle_path(scenario_dir))


def counts_from_collections(collections: dict[str, Any]) -> dict[str, int]:
    return {
        "truck": len(collections.get("truck") or {}),
        "order": len(collections.get("order") or {}),
        "facility": len(collections.get("facility") or {}),
    }


def preview_from_collections(collections: dict[str, Any]) -> dict[str, list]:
    """Small sample of truck/order agents for the dashboard detail view.

    Reuses ``frontend_scenario_spec.sample_agents`` (lazy import to avoid an
    import cycle — that module imports this one).
    """
    from .frontend_scenario_spec import sample_agents

    return {
        "truck": sample_agents(collections.get("truck"), _PREVIEW_TRUCK),
        "order": sample_agents(collections.get("order"), _PREVIEW_ORDER),
    }


def compile_bundle(
    collections: dict[str, Any],
    orsim_settings: dict[str, Any],
    recipe: Optional[dict[str, Any]] = None,
    *,
    domain: str,
    slug: str,
    name: Optional[str] = None,
    source: str = "generated",
    created_at: Optional[str] = None,
    behavior_revision: Optional[int] = None,
    generator_override_sha256: Optional[str] = None,
) -> dict[str, Any]:
    """Assemble the single ``scenario.json`` bundle dict (header first, agents last)."""
    agents = {key: (collections.get(key) or {}) for key in COLLECTION_KEYS}
    for key in OPTIONAL_COLLECTION_KEYS:
        agents[key] = collections.get(key) or {}
    if behavior_revision is None and isinstance(orsim_settings, dict):
        behavior_revision = orsim_settings.get("BEHAVIOR_REVISION")

    return {
        "schemaVersion": BUNDLE_SCHEMA_VERSION,
        "domain": domain,
        "slug": slug,
        "name": name or slug,
        "createdAt": created_at or datetime.now(timezone.utc).isoformat(),
        "source": source,
        "counts": counts_from_collections(collections),
        "preview": preview_from_collections(collections),
        "integrity": {
            "behaviorRevision": behavior_revision,
            "generatorOverrideSha256": generator_override_sha256,
        },
        "recipe": recipe,
        "settings": orsim_settings,
        "agents": agents,
    }


def write_bundle(scenario_dir: str, bundle: dict[str, Any]) -> str:
    """Write ``scenario.json`` atomically (tmp file + replace within the same dir)."""
    os.makedirs(scenario_dir, exist_ok=True)
    target = bundle_path(scenario_dir)
    tmp = target + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fp:
        json.dump(bundle, fp, indent=2, sort_keys=False)
        fp.write("\n")
    os.replace(tmp, target)  # atomic on the same filesystem
    return target


def read_bundle(scenario_dir: str) -> Optional[dict[str, Any]]:
    """Return the parsed ``scenario.json`` dict, or None if absent/corrupt."""
    path = bundle_path(scenario_dir)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fp:
            data = json.load(fp)
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def split_bundle(
    bundle: dict[str, Any], scenario_dir: str = ""
) -> tuple[dict[str, dict], dict, Optional[dict], dict[str, dict]]:
    """Pure splitter: an ALREADY-PARSED bundle -> ``(collections, settings, recipe, extras)``.

    Exposed separately so a caller holding the parsed dict never has to re-read the file.
    ``collections`` is exactly the original five-role dict; ``extras`` carries the optional
    later-added roles (``OPTIONAL_COLLECTION_KEYS``), ``{}`` per key for bundles predating them.
    """
    agents = bundle.get("agents")
    settings = bundle.get("settings")
    if not isinstance(agents, dict) or not isinstance(settings, dict):
        raise ValueError(f"Malformed {BUNDLE_FILENAME} in {scenario_dir}: missing agents/settings")
    collections = {key: (agents.get(key) or {}) for key in COLLECTION_KEYS}
    extras = {key: (agents.get(key) or {}) for key in OPTIONAL_COLLECTION_KEYS}
    recipe = bundle.get("recipe") if isinstance(bundle.get("recipe"), dict) else None
    return collections, settings, recipe, extras


def load_bundle_with_extras(
    scenario_dir: str,
) -> tuple[dict[str, dict], dict, Optional[dict], dict[str, dict]]:
    """``(collections, orsim_settings, recipe, extras)`` from **one** bundle parse.

    Callers that need the optional roles MUST use this rather than a second
    ``load_bundle_collection`` — re-reading re-parses a multi-hundred-MB JSON file on every
    run load (measured +1.9 s / +320 MB on the 165 MB consortium bundle, worse on the largest
    scenarios) in BOTH lifecycle modes.

    Raises ``FileNotFoundError`` when no bundle is present, ``ValueError`` when malformed.
    """
    bundle = read_bundle(scenario_dir)
    if bundle is None:
        raise FileNotFoundError(f"No {BUNDLE_FILENAME} in {scenario_dir}")
    return split_bundle(bundle, scenario_dir)


def load_bundle(scenario_dir: str) -> tuple[dict[str, dict], dict, Optional[dict]]:
    """Read the bundle and return ``(collections, orsim_settings, recipe)``.

    Public five-role contract — unchanged, and deliberately NOT widened as optional roles
    are added. Thin wrapper over :func:`load_bundle_with_extras` (still exactly one parse).
    """
    collections, settings, recipe, _extras = load_bundle_with_extras(scenario_dir)
    return collections, settings, recipe


def load_bundle_collection(scenario_dir: str, key: str) -> dict:
    """Fetch a single *optional* agent collection (see ``OPTIONAL_COLLECTION_KEYS``,
    e.g. ``"order_lifecycle"``) from the bundle.

    Returns ``{}`` when the bundle predates that role (no such key under ``agents``),
    when the collection itself is empty, or when there is no bundle at all — never raises.

    Convenience for standalone inspection/tests ONLY. It costs a full bundle parse, so a
    caller that is already loading the bundle (the run-load path) must use
    :func:`load_bundle_with_extras` instead.
    """
    bundle = read_bundle(scenario_dir)
    if not bundle:
        return {}
    agents = bundle.get("agents")
    if not isinstance(agents, dict):
        return {}
    return agents.get(key) or {}
