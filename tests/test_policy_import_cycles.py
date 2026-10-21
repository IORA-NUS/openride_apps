"""Import-cycle guard for the policy framework (plan docs/plans/policy_slots_p0_p3.md §2.7, §7 risk 2).

What is locked: every module under ``apps.container_logistics.policies``, plus
``assignment.app``, ``assignment.solver``, ``assignment.pooled_planner`` and
``assignment.accounting``, imports
cleanly as the FIRST import of a fresh interpreter; and the rule-7 leaf modules pull
no ``apps.container_logistics.assignment`` module into ``sys.modules``.

Why: ``assignment/__init__`` eagerly imports ``agent`` -> ``app`` (F19), so a cycle is
order-dependent and only shows when some module is imported first. An in-process
import cannot see it (the first test to import anything warms ``sys.modules``), hence
one subprocess per module.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
_POLICIES_DIR = REPO_ROOT / "apps" / "container_logistics" / "policies"
_EXTRA = [
    "apps.container_logistics.assignment.app",
    "apps.container_logistics.assignment.solver",
    "apps.container_logistics.assignment.pooled_planner",
    "apps.container_logistics.assignment.accounting",
]
# Plan §2.7 leaf list, plus dispatch/resolved.py (a value module the runner and
# dispatch/profile.py both import), and the round-2 leaves framework/components.py
# (registries imported locally) and dispatch/legacy.py (round-2 §4), plus
# dispatch/composite.py (it imports only base and resolved).
_LEAVES = [
    "apps.container_logistics.policies",
    "apps.container_logistics.policies.framework",
    "apps.container_logistics.policies.framework.errors",
    "apps.container_logistics.policies.framework.slots",
    "apps.container_logistics.policies.framework.params",
    "apps.container_logistics.policies.framework.registry",
    "apps.container_logistics.policies.framework.components",
    "apps.container_logistics.policies.framework.catalog",
    "apps.container_logistics.policies.dispatch.base",
    "apps.container_logistics.policies.dispatch.plan",
    "apps.container_logistics.policies.dispatch.pair_solver",
    "apps.container_logistics.policies.dispatch.resolved",
    "apps.container_logistics.policies.dispatch.legacy",
    "apps.container_logistics.policies.dispatch.composite",
]


def _policy_modules() -> list[str]:
    mods = []
    for path in sorted(_POLICIES_DIR.rglob("*.py")):
        rel = path.relative_to(REPO_ROOT).with_suffix("")
        parts = list(rel.parts)
        if parts[-1] == "__init__":
            parts = parts[:-1]
        mods.append(".".join(parts))
    return mods


def _run(code: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", code], cwd=REPO_ROOT, capture_output=True, text=True, timeout=120
    )


@pytest.mark.parametrize("module", _policy_modules() + _EXTRA)
def test_each_module_imports_cleanly_as_the_first_import(module):
    proc = _run(f"import {module}")
    assert proc.returncode == 0, f"`import {module}` as the first import failed:\n{proc.stderr[-3000:]}"


@pytest.mark.parametrize("module", _LEAVES)
def test_leaf_modules_pull_in_no_assignment_module(module):
    """Verified to have teeth: a top-level ``from apps.container_logistics.assignment
    import constraints`` added to ``dispatch/plan.py`` failed the plan and pair_solver cases.
    """
    proc = _run(
        f"import json, sys, {module}; "
        "print(json.dumps(sorted(m for m in sys.modules "
        "if m.startswith('apps.container_logistics.assignment'))))"
    )
    assert proc.returncode == 0, proc.stderr[-3000:]
    pulled = json.loads(proc.stdout.strip().splitlines()[-1])
    assert pulled == [], f"leaf module {module} imported assignment modules: {pulled}"


def test_the_walk_found_the_dispatch_modules():
    mods = set(_policy_modules())
    assert "apps.container_logistics.policies.dispatch.runner" in mods
    assert "apps.container_logistics.policies.dispatch.helpers.candidates" in mods
    # Round 2 C3: the whole-tick runtime's new modules are walked too.
    assert {"apps.container_logistics.policies.dispatch.compose",
            "apps.container_logistics.policies.dispatch.library.partitioned",
            "apps.container_logistics.policies.dispatch.library.pooled_market"} <= mods


def test_app_imports_the_dispatch_package_only_locally():
    """Round-2 §4: ``assignment/app.py`` may import ``assignment.accounting`` at top, but
    ``policies.dispatch`` only inside functions (it reaches back into ``assignment``).

    Verified to have teeth: moving ``app._dispatch_world``'s ``DispatchWorld`` import to the
    module top failed this test.
    """
    import ast

    tree = ast.parse((REPO_ROOT / "apps" / "container_logistics" / "assignment" / "app.py").read_text())
    top = [n for n in tree.body if isinstance(n, ast.ImportFrom)]
    leaks = [n.module for n in top if (n.module or "").startswith("apps.container_logistics.policies.dispatch")]
    assert leaks == [], f"app.py imports the dispatch package at module top: {leaks}"
    assert any(n.level == 1 and n.module is None and any(a.name == "accounting" for a in n.names)
               for n in top), "app.py imports assignment.accounting at top (the framework's accounting)"
