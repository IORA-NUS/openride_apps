"""Framework-import guard: what tier A may import, and which ``constraints`` functions it may use.

Plan ``plan_contract_final.md`` §7.3 (WS0), H2 and M11 (decision (f): the framework
never imports the solver-helper library). Tier A is the core runtime listed in
``tests/_framework_guard.py``. Three rules; END STATE (WS8): every tier-A module has ZERO
hits (the shrink-only per-module baselines WS0 started with, under
``tests/vocabulary_baseline/imports/``, are deleted; see ``_framework_guard``), and the
compat allow-list is exactly what tier A imports:

1. ``forbidden:<module>`` -- tier A may not import, by ANY spelling (``import``,
   ``from``, ``as``, a relative import, a local import inside a function, or
   ``importlib.import_module`` / ``__import__`` with a literal name), anything under
   :data:`FORBIDDEN_PACKAGES`.
2. ``compat:<module>:<symbol>`` -- from ``policies.compat`` tier A may import only the
   ``(module, symbol)`` pairs of :data:`COMPAT_ALLOWED`. Importing a compat MODULE object,
   a star import, or any other symbol is a hit. A new symbol needs a reviewed edit of the
   allow-list here, not a baseline line.
3. ``constraints:<name>`` -- tier A may reference only the ``assignment.constraints``
   attributes in :data:`CONSTRAINTS_ALLOWED` (the relabelling hole of H2: a framework
   module could otherwise re-read the cooperation schema through ``constraints``).
   ``declared_partnerships`` is the ONLY cooperation-schema read the framework keeps
   (decision (a)); WS1 adds it to ``constraints``. Every attribute reference counts,
   called or not.

Verified to have teeth (WS0, mutate / see it fail / restore):

- ``from apps.container_logistics.policies.solver_lib import x`` added INSIDE a function
  of ``dispatch/runner.py`` failed rule 1;
- ``from apps.container_logistics.policies.compat.legacy import nope`` in ``runner.py``
  failed rule 2;
- ``assign_constraints.pools_of_structure(None)`` added to ``assignment/app.py`` failed
  rule 3.

Verified to have teeth (WS8, end state): re-adding ``wrap_legacy_leaf`` to
``dispatch/profile.py``'s ``compat.legacy`` import failed ``test_tier_a_imports_obey_the_rules``
(``compat:legacy:wrap_legacy_leaf`` occurs 1x, allows 0).
"""

from __future__ import annotations

import ast
import sys
from collections import Counter
from pathlib import Path

import pytest

try:
    from tests import _framework_guard as guard
except ImportError:  # run as a script from the repo root
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from tests import _framework_guard as guard

_P = "apps.container_logistics.policies"
_A = "apps.container_logistics.assignment"

FORBIDDEN_PACKAGES = (
    f"{_P}.solver_lib",
    f"{_P}.dispatch.library",
    f"{_A}.solver",
    f"{_A}.pooled_planner",
    f"{_A}.pools",
    f"{_A}.policy",
    f"{_A}.arbitration",
)
COMPAT_PACKAGE = f"{_P}.compat"
# The END STATE (WS8) of what the framework takes from compat: exactly the pairs tier A
# imports (``test_every_compat_allowance_is_used`` keeps it so -- a stale allowance is a
# hole). Keys are module names relative to ``policies.compat``. Each entry says WHY the
# framework still needs an old-input / wire-format rule.
COMPAT_ALLOWED: dict[str, frozenset[str]] = {
    # dispatch/profile.py: the bundle-format marker check (a round-1 flat block is
    # refused at run start instead of silently running a different arm).
    "legacy": frozenset({"check_policies_format"}),
    # dispatch/profile.py: a profile with NO ``policies`` block (an old bundle) is
    # translated from its legacy keys. WS8 moved the whole translation here, replacing
    # the WS2 allowances ``wrap_legacy_leaf`` and ``LEGACY_LEAF_DEFAULT``.
    "legacy_profile": frozenset({"resolve_legacy_profile"}),
    # dispatch/profile.py: message 13, a compiled bundle's legacy mirrors (still written
    # by compile, bundle bytes unchanged) must agree with its ``policies`` block.
    "mirrors": frozenset({"check_mirrors"}),
    # dispatch/runner.py: an OFFLINE tick's measurement dials ("first node wins", plan
    # §2.5 M5). WS8 replaced ``accounting_params_for`` (a banned identifier) with this
    # framework-named entry; the rule itself stays in compat (plan §11 later phase).
    "measurement": frozenset({"offline_measurement_dials"}),
    # dispatch/bind.py: RunWorld excludes the legacy selection keys from the world.
    "selection_keys": frozenset({"LEGACY_SELECTION_KEYS"}),
    # dispatch/bind.py: a hand-injected solver with no @policy registration.
    "unregistered": frozenset({"UnregisteredPairLeaf", "legacy_spatial_rule"}),
    # dispatch/capture.py: reading format-/2 capture fixtures.
    "capture_v2": frozenset({"read_fixture_v2"}),
    # The wire formats that name old solver provenance: runner.py picks the tag format,
    # resolve.py stamps it, app.py builds the payload keywords, accounting.py dispatches
    # on the opaque format name (``share_tag_renderer``, reviewed WS3: spelling the
    # banned ``"v1"`` in accounting would be the alternative).
    "wire": frozenset({"share_tag_format_for", "stamp_fields", "wire_fields", "share_tag_renderer"}),
    # assignment/app.py: ``AssignmentApp.round_stats()``, which the frozen golden harness
    # calls (tests/golden/harness.py).
    "run_stats": frozenset({"LegacyAppAccessors"}),
    # dispatch/base.py, testing/battery.py: a legacy ``decide(ctx)`` class runs through
    # the adapter; ``legacy_world_reads`` (reviewed WS4b) makes DispatchPolicy's default
    # bind read the world keys the adapter reads for it, so the bind-reads rule
    # (battery contract.world_reads_at_bind) holds for legacy policies. Still needed
    # after WS8: legacy decide() classes (tests, third parties) are not shims.
    "legacy_context": frozenset({"LegacyDecideContext", "legacy_world_reads"}),
    # framework/slots.py: the slot default.
    "defaults": frozenset({"DEFAULT_PLANNER"}),
}
CONSTRAINTS_MODULE = f"{_A}.constraints"
CONSTRAINTS_ALLOWED = frozenset("""
    haulier_of haulier_matches pair_allowed pair_allowed_ignoring_haulier assignment_cost
    filter_assignable_trucks declared_partnerships
    truck_anchor_loc order_pickup_loc order_dropoff_loc _truck_last_dropoff_loc
    _truck_last_dropoff_facility _haversine_km size_compatible _DEFAULT_DUAL_CYCLE_RADIUS_KM
""".split())

IMPORTS_BASELINE_DIR = guard.BASELINE_DIR / "imports"


def _package_of(module: str, path: Path) -> str:
    return module if path.name == "__init__.py" else module.rpartition(".")[0]


def _resolve(node: ast.ImportFrom, package: str) -> str:
    if not node.level:
        return node.module or ""
    base = package.split(".")
    base = base[: len(base) - (node.level - 1)] if node.level > 1 else base
    return ".".join(base + ([node.module] if node.module else []))


def _under(name: str, package: str) -> bool:
    return name == package or name.startswith(package + ".")


def _dotted_attr(node: ast.AST) -> str | None:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        return ".".join([node.id] + parts[::-1])
    return None


def scan_imports(source: str, module: str, path: Path) -> Counter:
    """The import-rule hits of one tier-A module, as ``Counter({token: count})``."""
    tree = ast.parse(source)
    package = _package_of(module, path)
    hits: Counter = Counter()
    constraints_aliases: set[str] = set()   # local names bound to the constraints module

    def imported(target: str, *, symbol: str | None = None) -> None:
        """Record one import of ``target`` (a module), or of ``symbol`` from it."""
        full = f"{target}.{symbol}" if symbol and symbol != "*" else target
        for pkg in FORBIDDEN_PACKAGES:
            if _under(full, pkg) or _under(target, pkg):
                hits[f"forbidden:{pkg}"] += 1
                break
        if _under(full, COMPAT_PACKAGE):
            rel = target[len(COMPAT_PACKAGE) + 1:] if _under(target, COMPAT_PACKAGE) and target != COMPAT_PACKAGE else ""
            if not (symbol and rel and symbol in COMPAT_ALLOWED.get(rel, ())):
                hits[f"compat:{rel or full}:{symbol if rel else '<module>'}"] += 1
        if target == CONSTRAINTS_MODULE and symbol and symbol not in CONSTRAINTS_ALLOWED:
            hits[f"constraints:{symbol}"] += 1

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported(alias.name)
                if alias.name == CONSTRAINTS_MODULE and alias.asname:
                    constraints_aliases.add(alias.asname)
        elif isinstance(node, ast.ImportFrom):
            target = _resolve(node, package)
            for alias in node.names:
                imported(target, symbol=alias.name)
                if f"{target}.{alias.name}" == CONSTRAINTS_MODULE:
                    constraints_aliases.add(alias.asname or alias.name)
        elif isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant) \
                and isinstance(node.args[0].value, str):
            func = _dotted_attr(node.func)
            if func in ("importlib.import_module", "import_module", "__import__"):
                imported(node.args[0].value)

    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            owner = _dotted_attr(node.value)
            if owner and (owner in constraints_aliases or owner == CONSTRAINTS_MODULE) \
                    and node.attr not in CONSTRAINTS_ALLOWED:
                hits[f"constraints:{node.attr}"] += 1
    return hits


def scan_all() -> dict[str, Counter]:
    return {module: scan_imports(path.read_text(), module, path)
            for module, path in guard.tier_a_modules().items()}


_SCANS = scan_all()


@pytest.mark.parametrize("module", sorted(_SCANS))
def test_tier_a_imports_obey_the_rules(module):
    problems = guard.compare(module, _SCANS[module], guard.read_baseline(IMPORTS_BASELINE_DIR / f"{module}.txt"))
    assert not problems, "\n".join(problems)


def _compat_imports() -> set[tuple[str, str]]:
    """Every ``(compat module, symbol)`` a tier-A module imports."""
    used: set[tuple[str, str]] = set()
    for module, path in guard.tier_a_modules().items():
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom):
                target = _resolve(node, _package_of(module, path))
                if _under(target, COMPAT_PACKAGE) and target != COMPAT_PACKAGE:
                    used |= {(target[len(COMPAT_PACKAGE) + 1:], a.name) for a in node.names}
    return used


def test_every_compat_allowance_is_used():
    """The allow-list is the END STATE: exactly what tier A imports, nothing spare (WS8).

    Verified to have teeth (WS8): re-adding ``wrap_legacy_leaf`` to ``"legacy"`` failed it.
    """
    allowed = {(m, sym) for m, syms in COMPAT_ALLOWED.items() for sym in syms}
    assert allowed - _compat_imports() == set(), "allow-listed but never imported (a stale hole)"


_RUNNER = Path(guard.REPO_ROOT, "apps/container_logistics/policies/dispatch/runner.py")
_RUNNER_MOD = "apps.container_logistics.policies.dispatch.runner"


@pytest.mark.parametrize(("source", "want"), [
    ("def f():\n    from apps.container_logistics.policies.solver_lib import x",
     {"forbidden:apps.container_logistics.policies.solver_lib": 1}),
    ("from apps.container_logistics.policies import solver_lib as s",
     {"forbidden:apps.container_logistics.policies.solver_lib": 1}),
    ("from ..solver_lib.candidates import spatial_pairs",
     {"forbidden:apps.container_logistics.policies.solver_lib": 1}),
    ("from .library import partitioned", {"forbidden:apps.container_logistics.policies.dispatch.library": 1}),
    ("import apps.container_logistics.assignment.pooled_planner as pp",
     {"forbidden:apps.container_logistics.assignment.pooled_planner": 1}),
    ("import importlib\nm = importlib.import_module('apps.container_logistics.assignment.solver.greedy')",
     {"forbidden:apps.container_logistics.assignment.solver": 1}),
    ("from apps.container_logistics.assignment import policy_stub", {}),   # prefix at a dot boundary only
    ("from apps.container_logistics.policies.compat.wire import wire_fields", {}),
    ("from ..compat.defaults import DEFAULT_PLANNER", {}),
    ("from ..compat.legacy import nope", {"compat:legacy:nope": 1}),
    ("from ..compat.legacy import *", {"compat:legacy:*": 1}),
    ("from .. import compat", {"compat:apps.container_logistics.policies.compat:<module>": 1}),
    ("from apps.container_logistics.assignment import constraints as c\nc.haulier_of(t)\nc.share_adjacency(s)",
     {"constraints:share_adjacency": 1}),
    ("from apps.container_logistics.assignment.constraints import share_eligible, haulier_of",
     {"constraints:share_eligible": 1}),
])
def test_the_import_scanner_catches_every_spelling(source, want):
    assert dict(scan_imports(source, _RUNNER_MOD, _RUNNER)) == want


def test_the_compat_allow_list_names_only_compat_modules():
    """Every allow-listed module is a bare compat sibling name (no dots, no solver packages)."""
    assert all("." not in m for m in COMPAT_ALLOWED)
    assert not CONSTRAINTS_ALLOWED & {"active_cooperation_structure", "share_adjacency", "share_eligible"}
