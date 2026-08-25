"""Optional per-scenario datagen override loader.

A scenario folder may contain a single ``scenario_gen.py`` that customizes
generation **for that one scenario** without copying the shared engine. It may
define any subset of these optional hooks (everything it omits falls back to the
shared default):

    customize_spec(spec) -> GenerationSpec
        Adjust the frozen recipe in Python (e.g. computed counts/matrix).
    customize_catalog(catalog, spec) -> LocationCatalog
        Add or modify location sites (e.g. a site not in the address CSV via
        ``catalog.add_manual_site(...)``).
    BUILDERS = {"order": MyOrderBuilder, ...}
        Swap one or more role builder classes (same ``(spec, catalog, rng)`` /
        ``build(agent_id, ...)`` contract as the defaults).
    post_generate(result, spec) -> GenerationResult
        Final mutation of the assembled collections.

Security: this file is imported **only inside the trusted host generation
subprocess** (the apps venv) — never in the web server or a Celery agent. The
frontend generate path cannot create one (it only POSTs a JSON spec), so
untrusted input can't inject code here.
"""

from __future__ import annotations

import hashlib
import importlib.util
import os
from typing import Optional

OVERRIDE_FILENAME = "scenario_gen.py"


def override_path(scenario_dir: str) -> str:
    return os.path.join(scenario_dir, OVERRIDE_FILENAME)


def has_override(scenario_dir: str) -> bool:
    return os.path.isfile(override_path(scenario_dir))


def override_sha256(scenario_dir: str) -> Optional[str]:
    """sha256 of the override file (recorded in the bundle for reproducibility)."""
    path = override_path(scenario_dir)
    if not os.path.isfile(path):
        return None
    with open(path, "rb") as fp:
        return hashlib.sha256(fp.read()).hexdigest()


def load_scenario_overrides(scenario_dir: str):
    """Import ``scenario_gen.py`` from the scenario folder, or return None if absent.

    Raises (loudly) if the file exists but fails to import — a broken override must
    never silently fall back to default generation.
    """
    path = override_path(scenario_dir)
    if not os.path.isfile(path):
        return None
    spec = importlib.util.spec_from_file_location("scenario_gen_override", path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
