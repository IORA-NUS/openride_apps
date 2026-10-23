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

Final review M9 (fix group FB) closed five holes:

- the compat allow-list is PER MODULE (:data:`COMPAT_ALLOWED`: which tier-A module may
  take which compat symbol), so one module's reviewed need no longer licenses every
  other module;
- tier A is BY DIRECTORY (``_framework_guard.TIER_A_DIRS``), so a new framework file is
  scanned without anyone listing it;
- rule 4, ``dynamic:<what>`` -- tier A may not use ``importlib``, ``__import__``,
  ``sys.modules`` or ``builtins`` (a dynamic import is invisible to rules 1-3), except
  the modules of :data:`DYNAMIC_IMPORT_ALLOWED`, each with its reason;
- attribute traversal through a module alias (``import apps.container_logistics.policies
  as p; p.solver_lib.x``) is resolved and checked like an import;
- re-export laundering (``from <innocent module> import <a solver symbol>``) is caught at
  run time (:func:`laundered_imports`: the imported object's own ``__module__`` /
  ``__name__`` decides, not the module it was taken from).

The TRANSITIVE reach of tier A into solver packages is decided and pinned
(:data:`TRANSITIVE_SOLVER_GATEWAYS`, :func:`test_the_transitive_solver_reach_is_exactly_the_documented_gateways`):
a static import closure from every tier-A module, expanding through every ``apps.*``
module it reaches (package ``__init__`` files included), stops at the first forbidden
package and records the module whose import crossed. Exactly four modules may cross,
three in ``policies/compat/`` (the quarantine, whose job is to serve old solvers) and
the tier-B fixture builder.

Verified to have teeth (FB; mutate / see it fail / restore):

- a new file ``policies/dispatch/_probe.py`` with ``from ..solver_lib import candidates``
  failed ``test_tier_a_imports_obey_the_rules[...dispatch._probe]`` with no tier edit
  (tier by directory);
- ``from apps.container_logistics.policies.compat.wire import wire_fields`` added to
  ``dispatch/runner.py`` (a symbol ``assignment/app.py`` may take, ``runner.py`` may
  not) failed it (``compat:wire:wire_fields``) -- under the old global allow-list it
  passed;
- ``import importlib`` added to ``dispatch/tick.py`` failed it (``dynamic:importlib``);
- ``from apps.container_logistics.policies.solver_lib import candidates`` added to
  ``framework/catalog.py`` failed it AND the transitive-gateway test (a new crossing);
- ``from apps.container_logistics.policies.solver_lib import provenance`` added to
  ``compat/wire.py`` (compat is never scanned, so no tier-A scan changes) failed the
  transitive-gateway test alone: ``wire`` became a fifth, undocumented gateway.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import sys
from collections import Counter
from pathlib import Path
from types import ModuleType

import pytest

try:
    from tests import _framework_guard as guard
except ImportError:  # run as a script from the repo root
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from tests import _framework_guard as guard

_P = "apps.container_logistics.policies"
_A = "apps.container_logistics.assignment"

#: The solver packages (one list, shared with the tier-A closure, ``_framework_guard``).
FORBIDDEN_PACKAGES = guard.SOLVER_PACKAGES
COMPAT_PACKAGE = guard.COMPAT_PACKAGE
# The END STATE of what each tier-A module takes from compat: exactly the pairs it
# imports (``test_every_compat_allowance_is_used`` keeps it so -- a stale allowance is a
# hole). PER MODULE since the final review (M9): ``{tier-A module: {compat module (relative
# to policies.compat): {symbols}}}``. Each entry says WHY that module still needs an
# old-input / wire-format rule.
COMPAT_ALLOWED: dict[str, dict[str, frozenset[str]]] = {
    # The bundle-format marker check (a round-1 flat block is refused at run start);
    # a profile with NO ``policies`` block is translated from its legacy keys; a
    # legacy ``use_spatial_matching`` next to a block is refused (moved out of profile.py,
    # M8); message 13, a bundle's legacy mirrors must agree with its block.
    f"{_P}.dispatch.profile": {
        "legacy": frozenset({"check_policies_format"}),
        "legacy_profile": frozenset({"resolve_legacy_profile", "refuse_legacy_flag_next_to_block"}),
        "mirrors": frozenset({"check_mirrors"}),
    },
    # An OFFLINE tick's measurement dials ("first node wins", plan §2.5 M5, the user-approved
    # quarantined rule of plan §0 item 3(b)); the tag format of the top (``v1`` for exactly
    # the pre-v2 Partitioned top, a name-keyed compat rule).
    f"{_P}.dispatch.runner": {
        "measurement": frozenset({"offline_measurement_dials"}),
        "wire": frozenset({"share_tag_format_for"}),
    },
    # RunWorld excludes the legacy selection keys from the world; a hand-injected solver
    # with no @policy registration is served as a pair leaf.
    f"{_P}.dispatch.bind": {
        "selection_keys": frozenset({"LEGACY_SELECTION_KEYS"}),
        "unregistered": frozenset({"UnregisteredPairLeaf", "legacy_spatial_rule"}),
    },
    # Reading format-/2 capture fixtures.
    f"{_P}.dispatch.capture": {"capture_v2": frozenset({"read_fixture_v2"})},
    # A legacy ``decide(ctx)`` class runs through the adapter, which also owns its old
    # declaration: the world keys it reads and the round-2 candidate hint (M7).
    f"{_P}.dispatch.base": {
        "legacy_context": frozenset({"LegacyDecideContext", "legacy_world_reads", "legacy_candidate_hint"}),
    },
    # The battery's legacy-adapter check of a legacy ``decide`` policy.
    f"{_P}.testing.battery": {"legacy_context": frozenset({"LegacyDecideContext"})},
    # The run stamp's compat entry (``share_tag_format``).
    f"{_P}.framework.resolve": {"wire": frozenset({"stamp_fields"})},
    # The slot default.
    f"{_P}.framework.slots": {"defaults": frozenset({"DEFAULT_PLANNER"})},
    # ``publish()`` builds the payload keywords from a tag; ``AssignmentApp.round_stats()``,
    # which the frozen golden harness calls (tests/golden/harness.py).
    f"{_A}.app": {
        "wire": frozenset({"wire_fields"}),
        "run_stats": frozenset({"LegacyAppAccessors"}),
    },
    # The accounting dispatches on the opaque format name (reviewed WS3).
    f"{_A}.accounting": {"wire": frozenset({"share_tag_renderer"})},
}
CONSTRAINTS_MODULE = f"{_A}.constraints"
CONSTRAINTS_ALLOWED = frozenset("""
    haulier_of haulier_matches pair_allowed pair_allowed_ignoring_haulier assignment_cost
    filter_assignable_trucks declared_partnerships
    truck_anchor_loc order_pickup_loc order_dropoff_loc _truck_last_dropoff_loc
    _truck_last_dropoff_facility _haversine_km size_compatible _DEFAULT_DUAL_CYCLE_RADIUS_KM
""".split())
#: Rule 4's exceptions: tier-A modules that may import dynamically, each with its reason.
DYNAMIC_IMPORT_ALLOWED: dict[str, str] = {
    f"{_P}.framework.registry": (
        "built-in discovery (discover_builtin_policies imports BUILTIN_POLICY_PACKAGES, the ONE "
        "sanctioned dynamic import) and PolicySpec.missing_requirements (importlib.util.find_spec of "
        "a policy's declared requires)"),
}
#: The tier-A import closure's crossings into a forbidden package, by the crossing
#: module -> the forbidden modules it imports, each with the reason it is allowed.
TRANSITIVE_SOLVER_GATEWAYS: dict[str, tuple[frozenset[str], str]] = {
    f"{_P}.compat.legacy_context": (
        frozenset({f"{_P}.solver_lib", f"{_P}.solver_lib.candidates", f"{_P}.solver_lib.objective"}),
        "compat: a legacy decide(ctx) class's context serves candidate pairs and dual-cycle costs from "
        "the solver helpers it was written against (reached via dispatch/base.py, testing/battery.py)"),
    f"{_P}.compat.run_stats": (
        frozenset({f"{_P}.dispatch.library", f"{_P}.dispatch.library._market_stats"}),
        "compat: AssignmentApp.round_stats() for the frozen golden harness reads the market's run "
        "stats (reached via assignment/app.py)"),
    f"{_P}.compat.unregistered": (
        frozenset({f"{_P}.solver_lib", f"{_P}.solver_lib.pair_leaf", f"{_P}.solver_lib.pair_solver"}),
        "compat: a hand-injected, unregistered solver is served as a built-in pair leaf (reached via "
        "dispatch/bind.py)"),
    f"{_P}.testing.fixtures": (
        frozenset({f"{_P}.solver_lib", f"{_P}.solver_lib.candidates"}),
        "tier B (world plumbing, _framework_guard.TIER_B_OVERRIDES): a fixture writes the four "
        "candidate knobs into its profile, and their defaults are owned by solver_lib.candidates "
        "(CandidateSettings); duplicating them would drift (reached via testing/battery.py, bench.py)"),
}

IMPORTS_BASELINE_DIR = guard.BASELINE_DIR / "imports"


_package_of = guard.package_of
_resolve = guard.resolve_from
_under = guard.under
_module_path = guard.module_path
_static_imports = guard.static_imports


def _dotted_attr(node: ast.AST) -> str | None:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        return ".".join([node.id] + parts[::-1])
    return None


def _compat_rel(target: str) -> str:
    return target[len(COMPAT_PACKAGE) + 1:] if _under(target, COMPAT_PACKAGE) and target != COMPAT_PACKAGE else ""


_DYNAMIC_NAMES = {"__import__": "__import__", "import_module": "import_module"}


def scan_imports(source: str, module: str, path: Path) -> Counter:
    """The import-rule hits of one tier-A module, as ``Counter({token: count})``."""
    tree = ast.parse(source)
    package = _package_of(module, path)
    hits: Counter = Counter()
    constraints_aliases: set[str] = set()   # local names bound to the constraints module
    module_aliases: dict[str, str] = {}     # local name -> the module (or package) it is bound to
    sys_aliases: set[str] = set()
    allowed_compat = COMPAT_ALLOWED.get(module, {})

    def imported(target: str, *, symbol: str | None = None) -> None:
        """Record one import of ``target`` (a module), or of ``symbol`` from it."""
        full = f"{target}.{symbol}" if symbol and symbol != "*" else target
        for pkg in FORBIDDEN_PACKAGES:
            if _under(full, pkg) or _under(target, pkg):
                hits[f"forbidden:{pkg}"] += 1
                break
        if _under(full, COMPAT_PACKAGE):
            rel = _compat_rel(target)
            if not (symbol and rel and symbol in allowed_compat.get(rel, ())):
                hits[f"compat:{rel or full}:{symbol if rel else '<module>'}"] += 1
        if target == CONSTRAINTS_MODULE and symbol and symbol not in CONSTRAINTS_ALLOWED:
            hits[f"constraints:{symbol}"] += 1
        if _under(target, "importlib") or _under(target, "builtins"):
            hits[f"dynamic:{target.partition('.')[0]}"] += 1

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported(alias.name)
                if alias.name == CONSTRAINTS_MODULE and alias.asname:
                    constraints_aliases.add(alias.asname)
                if alias.name == "sys":
                    sys_aliases.add(alias.asname or "sys")
                if alias.asname:
                    module_aliases[alias.asname] = alias.name
                else:
                    module_aliases[alias.name.partition(".")[0]] = alias.name.partition(".")[0]
        elif isinstance(node, ast.ImportFrom):
            target = _resolve(node, package)
            for alias in node.names:
                imported(target, symbol=alias.name)
                if f"{target}.{alias.name}" == CONSTRAINTS_MODULE:
                    constraints_aliases.add(alias.asname or alias.name)
                if target == "sys" and alias.name == "modules":
                    hits["dynamic:sys.modules"] += 1
                if _module_path(f"{target}.{alias.name}") is not None:
                    module_aliases[alias.asname or alias.name] = f"{target}.{alias.name}"
        elif isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant) \
                and isinstance(node.args[0].value, str):
            func = _dotted_attr(node.func)
            if func in ("importlib.import_module", "import_module", "__import__"):
                imported(node.args[0].value)
        if isinstance(node, ast.Name) and node.id in _DYNAMIC_NAMES:
            hits[f"dynamic:{_DYNAMIC_NAMES[node.id]}"] += 1

    # Attribute chains: the constraints rule, ``sys.modules``, and traversal through a
    # module alias (``p.solver_lib.x`` after ``import apps.container_logistics.policies as p``).
    inner_values = {id(n.value) for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        owner = _dotted_attr(node.value)
        if owner and (owner in constraints_aliases or owner == CONSTRAINTS_MODULE) \
                and node.attr not in CONSTRAINTS_ALLOWED:
            hits[f"constraints:{node.attr}"] += 1
        if owner in sys_aliases and node.attr == "modules":
            hits["dynamic:sys.modules"] += 1
        if id(node) in inner_values:
            continue                       # only the outermost chain is resolved
        chain = _dotted_attr(node)
        if not chain:
            continue
        root, _, rest = chain.partition(".")
        if root not in module_aliases or not rest:
            continue
        resolved = f"{module_aliases[root]}.{rest}"
        if any(_under(resolved, pkg) and not _under(module_aliases[root], pkg) for pkg in FORBIDDEN_PACKAGES) or \
                (_under(resolved, COMPAT_PACKAGE) and not _under(module_aliases[root], COMPAT_PACKAGE)):
            parts = resolved.split(".")
            for i in range(len(parts), 0, -1):    # the longest real-module prefix, then its symbol
                prefix = ".".join(parts[:i])
                if _module_path(prefix) is not None or i == 1:
                    imported(prefix, symbol=parts[i] if i < len(parts) else None)
                    break
    if module in DYNAMIC_IMPORT_ALLOWED:
        for key in [k for k in hits if k.startswith("dynamic:")]:
            del hits[key]
    return hits


def laundered_imports(source: str, module: str, path: Path) -> Counter:
    """``from M import S`` whose OBJECT comes from a forbidden or compat package while ``M`` does not.

    Run time: ``M`` is imported and ``S``'s own ``__module__`` (``__name__`` for a module)
    decides. Constants without a ``__module__`` are not attributed. Hits are
    ``laundered:<M>.<S><-<origin>``.
    """
    hits: Counter = Counter()
    package = _package_of(module, path)
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.ImportFrom):
            continue
        target = _resolve(node, package)
        if not target.startswith("apps.") or _under(target, COMPAT_PACKAGE) \
                or any(_under(target, pkg) for pkg in FORBIDDEN_PACKAGES):
            continue                       # a direct import: rules 1 and 2 decide it
        try:
            source_module = importlib.import_module(target)
        except Exception:                  # noqa: BLE001 - not importable here: nothing to attribute
            continue
        for alias in node.names:
            if alias.name == "*":
                continue
            obj = getattr(source_module, alias.name, None)
            origin = obj.__name__ if isinstance(obj, ModuleType) else getattr(obj, "__module__", None)
            if not isinstance(origin, str) or not (inspect.isclass(obj) or inspect.isfunction(obj)
                                                   or isinstance(obj, ModuleType)):
                continue
            if any(_under(origin, pkg) for pkg in FORBIDDEN_PACKAGES) or _under(origin, COMPAT_PACKAGE):
                hits[f"laundered:{target}.{alias.name}<-{origin}"] += 1
    return hits


def scan_all() -> dict[str, Counter]:
    return {module: scan_imports(path.read_text(), module, path) + laundered_imports(path.read_text(), module, path)
            for module, path in guard.tier_a_modules().items()}


_SCANS = scan_all()


@pytest.mark.parametrize("module", sorted(_SCANS))
def test_tier_a_imports_obey_the_rules(module):
    problems = guard.compare(module, _SCANS[module], guard.read_baseline(IMPORTS_BASELINE_DIR / f"{module}.txt"))
    assert not problems, "\n".join(problems)


def _compat_imports() -> set[tuple[str, str, str]]:
    """Every ``(tier-A module, compat module, symbol)`` a tier-A module imports."""
    used: set[tuple[str, str, str]] = set()
    for module, path in guard.tier_a_modules().items():
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom):
                target = _resolve(node, _package_of(module, path))
                if _under(target, COMPAT_PACKAGE) and target != COMPAT_PACKAGE:
                    used |= {(module, _compat_rel(target), a.name) for a in node.names}
    return used


def test_every_compat_allowance_is_used():
    """The allow-list is the END STATE: exactly what each tier-A module imports, nothing spare.

    Verified to have teeth (WS8): re-adding ``wrap_legacy_leaf`` to ``"legacy"`` failed it.
    """
    allowed = {(m, cm, sym) for m, mods in COMPAT_ALLOWED.items() for cm, syms in mods.items() for sym in syms}
    assert allowed - _compat_imports() == set(), "allow-listed but never imported (a stale hole)"
    assert set(COMPAT_ALLOWED) <= set(guard.tier_a_modules()), "an allowance for a module that is not tier A"


def test_every_dynamic_import_allowance_is_used_and_tier_a():
    for module in DYNAMIC_IMPORT_ALLOWED:
        path = guard.tier_a_modules()[module]
        source = path.read_text()
        assert "importlib" in source or "__import__" in source, f"{module}: stale dynamic-import allowance"


# --- the transitive reach ---------------------------------------------------------------

def transitive_solver_crossings() -> dict[str, set[str]]:
    """``{crossing module: {forbidden modules it imports}}`` over the static closure of tier A."""
    seen: set[str] = set()
    stack = sorted(guard.tier_a_modules())
    crossings: dict[str, set[str]] = {}
    while stack:
        module = stack.pop()
        if module in seen:
            continue
        seen.add(module)
        path = _module_path(module)
        if path is None:
            continue
        for target in _static_imports(module, path):
            if any(_under(target, pkg) for pkg in FORBIDDEN_PACKAGES):
                crossings.setdefault(module, set()).add(target)
            elif target not in seen:
                stack.append(target)
    return crossings


def test_the_transitive_solver_reach_is_exactly_the_documented_gateways():
    """Decided (M9): tier A reaches solver code ONLY through these four modules, for these reasons."""
    got = transitive_solver_crossings()
    want = {m: set(targets) for m, (targets, _why) in TRANSITIVE_SOLVER_GATEWAYS.items()}
    assert got == want, (f"tier A's transitive reach into solver packages changed: new {sorted(set(got) - set(want))}, "
                         f"gone {sorted(set(want) - set(got))}; a new crossing needs a reviewed gateway entry")
    for module in TRANSITIVE_SOLVER_GATEWAYS:
        assert _under(module, COMPAT_PACKAGE) or module in guard.tier_b_modules(), (
            f"{module}: a gateway must be compat (the quarantine) or tier B, never tier A")


_RUNNER = Path(guard.REPO_ROOT, "apps/container_logistics/policies/dispatch/runner.py")
_RUNNER_MOD = "apps.container_logistics.policies.dispatch.runner"
_APP = Path(guard.REPO_ROOT, "apps/container_logistics/assignment/app.py")
_APP_MOD = "apps.container_logistics.assignment.app"
_REGISTRY = Path(guard.REPO_ROOT, "apps/container_logistics/policies/framework/registry.py")
_REGISTRY_MOD = "apps.container_logistics.policies.framework.registry"


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
     {"forbidden:apps.container_logistics.assignment.solver": 1, "dynamic:importlib": 1}),
    ("from apps.container_logistics.assignment import policy_stub", {}),   # prefix at a dot boundary only
    ("from ..compat.measurement import offline_measurement_dials", {}),
    ("from ..compat.legacy import nope", {"compat:legacy:nope": 1}),
    ("from ..compat.legacy import *", {"compat:legacy:*": 1}),
    ("from .. import compat", {"compat:apps.container_logistics.policies.compat:<module>": 1}),
    ("from apps.container_logistics.assignment import constraints as c\nc.haulier_of(t)\nc.share_adjacency(s)",
     {"constraints:share_adjacency": 1}),
    ("from apps.container_logistics.assignment.constraints import share_eligible, haulier_of",
     {"constraints:share_eligible": 1}),
    # M9: the allow-list is per module -- app.py's wire_fields is not runner.py's.
    ("from apps.container_logistics.policies.compat.wire import wire_fields", {"compat:wire:wire_fields": 1}),
    ("from ..compat.defaults import DEFAULT_PLANNER", {"compat:defaults:DEFAULT_PLANNER": 1}),
    # M9: dynamic imports.
    ("import importlib", {"dynamic:importlib": 1}),
    ("from importlib import import_module\nm = import_module(name)", {"dynamic:importlib": 1, "dynamic:import_module": 1}),
    ("m = __import__(name)", {"dynamic:__import__": 1}),
    ("import sys\nm = sys.modules[name]", {"dynamic:sys.modules": 1}),
    ("import sys as _s\nm = _s.modules.get(name)", {"dynamic:sys.modules": 1}),
    ("from sys import modules", {"dynamic:sys.modules": 1}),
    ("import builtins\nbuiltins.__dict__", {"dynamic:builtins": 1}),
    ("import sys\nsys.path.insert(0, '.')", {}),
    # M9: attribute traversal through a module alias.
    ("import apps.container_logistics.policies as p\np.solver_lib.candidates.spatial_pairs",
     {"forbidden:apps.container_logistics.policies.solver_lib": 1}),
    ("from apps.container_logistics import policies\npolicies.dispatch.library.partitioned",
     {"forbidden:apps.container_logistics.policies.dispatch.library": 1}),
    ("import apps.container_logistics\napps.container_logistics.policies.compat.legacy.wrap_legacy_leaf",
     {"compat:legacy:wrap_legacy_leaf": 1}),
    ("from apps.container_logistics.policies import dispatch\ndispatch.tick.Tick", {}),
])
def test_the_import_scanner_catches_every_spelling(source, want):
    assert dict(scan_imports(source, _RUNNER_MOD, _RUNNER)) == want


def test_the_allow_lists_are_per_module():
    """``assignment/app.py`` may take ``wire_fields``; the registry may import dynamically. Nobody else."""
    assert dict(scan_imports("from apps.container_logistics.policies.compat.wire import wire_fields",
                             _APP_MOD, _APP)) == {}
    assert dict(scan_imports("import importlib\nimportlib.import_module(p)", _REGISTRY_MOD, _REGISTRY)) == {}


def test_re_export_laundering_is_caught(monkeypatch):
    """``from <innocent module> import <a solver class>`` is attributed to the class's own module."""
    from apps.container_logistics.policies.solver_lib.pair_solver import PairSolver

    probe = ModuleType("apps.container_logistics.policies.dispatch._launder_probe")
    probe.PairSolver = PairSolver
    probe.CONSTANT = ("not", "attributed")
    monkeypatch.setitem(sys.modules, probe.__name__, probe)
    src = "from apps.container_logistics.policies.dispatch._launder_probe import PairSolver, CONSTANT"
    assert dict(laundered_imports(src, _RUNNER_MOD, _RUNNER)) == {
        "laundered:apps.container_logistics.policies.dispatch._launder_probe.PairSolver"
        "<-apps.container_logistics.policies.solver_lib.pair_solver": 1}
    assert dict(scan_imports(src, _RUNNER_MOD, _RUNNER)) == {}, "the static scan alone cannot see it"


def test_the_compat_allow_list_names_only_compat_modules():
    """Every allow-listed compat module is a bare compat sibling name (no dots, no solver packages)."""
    assert all("." not in cm for mods in COMPAT_ALLOWED.values() for cm in mods)
    assert not CONSTRAINTS_ALLOWED & {"active_cooperation_structure", "share_adjacency", "share_eligible"}
