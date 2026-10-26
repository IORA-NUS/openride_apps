"""Shared machinery of the framework-vocabulary and framework-import guards (END STATE).

Plan ``plan_contract_final.md`` §7.3 (WS0) and §7.12 (WS8). Two tests use it:

- ``tests/test_framework_vocabulary.py`` -- the guarded modules contain NO solver
  vocabulary (``pooled``, ``market``, ``PartitionRule``, ...);
- ``tests/test_framework_imports.py`` -- tier A imports no solver package, takes only
  allow-listed symbols from ``policies.compat``, and calls only allow-listed
  ``assignment.constraints`` functions.

History: WS0 started both guards with per-module, shrink-only BASELINE files
(``tests/vocabulary_baseline/``), and a workstream could add a transitional line only
with a ``# removed-by: WS<n>`` tag naming a later workstream (plan H4). WS8 is the last
workstream: it removed the last hits, deleted the baseline directory and set
:data:`REQUIRE_EMPTY_BASELINES`. Every guarded module must now scan to ZERO hits, and
the only exemption is ``policies/compat/**`` (never scanned, :func:`_expand`).

Tiers are BY DIRECTORY since the final review (M9): every module under
``policies/framework/``, ``policies/dispatch/`` (minus ``library/``) and
``policies/testing/`` is tier A unless :data:`TIER_B_OVERRIDES` names it, with a reason.

THE COMPAT EXEMPTION IS ONE PREFIX (final review round 2, G1): exactly
``apps/container_logistics/policies/compat/`` (:data:`COMPAT_PREFIX`). A directory named
``compat`` anywhere else (``dispatch/compat/``, ``framework/compat/``) is scanned like any
other; the old ``"/compat/" in path`` test exempted all of them.

TIER A INCLUDES ITS STATIC IMPORT CLOSURE (round 2, G2): every ``apps.*`` module a tier-A
module imports (local imports and parent packages included, as Python executes them),
transitively, is tier A too, so a new sibling such as ``assignment/publish_helpers.py``
imported by ``app.py`` is scanned the moment it is imported. The closure stops (does not
include, does not expand) at :data:`CLOSURE_STOP_PACKAGES` (the compat quarantine and the
solver packages, whose crossing the import guard reports), at tier-B modules, and at the
reviewed :data:`CLOSURE_BOUNDARY`, each entry with its reason. A boundary entry is an
exact module name, never a prefix.

Re-creating a baseline file can not launder a hit: :func:`compare` reports every line
of one (``REQUIRE_EMPTY_BASELINES``), and every ``# removed-by:`` tag names a merged
workstream (:data:`MERGED_WORKSTREAMS`), so it is reported as stale too.
"""

from __future__ import annotations

import ast
import functools
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping

REPO_ROOT = Path(__file__).resolve().parents[1]
BASELINE_DIR = REPO_ROOT / "tests" / "vocabulary_baseline"

# Every workstream of the build (WS6 is the frontend repo). A ``# removed-by: WS<n>`` tag
# naming one of these is stale; with WS8 in, every tag is.
MERGED_WORKSTREAMS: frozenset[str] = frozenset({"WS0", "WS1", "WS2", "WS3", "WS4a", "WS4b", "WS5", "WS7", "WS8"})
# WS8 flipped this: there are no baselines; every guarded module scans to zero hits.
REQUIRE_EMPTY_BASELINES = True

_POLICIES = "apps/container_logistics/policies"
#: The ONE exempt subtree (G1): never scanned, never in a tier, never in the closure.
COMPAT_PREFIX = f"{_POLICIES}/compat"
_P = "apps.container_logistics.policies"
_A = "apps.container_logistics.assignment"
COMPAT_PACKAGE = f"{_P}.compat"
#: Solver-side packages. The import guard forbids tier A to import any of them (its
#: ``FORBIDDEN_PACKAGES`` is this tuple); the closure does not enter them.
SOLVER_PACKAGES: tuple[str, ...] = (
    f"{_P}.solver_lib",
    f"{_P}.dispatch.library",
    f"{_A}.solver",
    f"{_A}.pooled_planner",
    f"{_A}.pools",
    f"{_A}.policy",
    f"{_A}.arbitration",
)
CLOSURE_STOP_PACKAGES: tuple[str, ...] = (COMPAT_PACKAGE, *SOLVER_PACKAGES)
#: Modules tier A imports that are NOT framework code, so the closure stops there (they
#: are neither scanned as tier A nor expanded). Exact module names; each with its reason.
CLOSURE_BOUNDARY: dict[str, str] = {
    f"{_A}.constraints": (
        "the world schema (cooperation structures, haulier ids, locations): its vocabulary IS the "
        "world's. Tier A's use of it is pinned by the import guard's rule 3 (CONSTRAINTS_ALLOWED)"),
    "apps.container_logistics.message_data_models": (
        "the published wire schema of every payload (the assignment payload's share fields are "
        "read by trucks and analytics); tier A fills it only through compat.wire"),
    "apps.agent_transport": (
        "ecosystem-wide transport selection (imports the chosen broker backend dynamically), "
        "reached from the ORSim agent shell assignment/agent.py; no dispatch decision"),
}

# TIERS BY DIRECTORY (final review M9): every ``.py`` under a tier directory is
# scanned, recursively, so a NEW framework file is covered the moment it exists --
# nobody has to remember to list it. ``exclude`` cuts out solver-side subtrees. Tier A:
# core runtime. Tier B: world plumbing. ``policies/compat/**`` is never scanned
# (:func:`_expand`), whatever a glob says.
TIER_A_DIRS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (f"{_POLICIES}/framework", ()),
    (f"{_POLICIES}/dispatch", (f"{_POLICIES}/dispatch/library",)),
    (f"{_POLICIES}/testing", ()),
)
# Single framework files that live in a solver-mixed directory (``assignment/`` also
# holds the built-in solvers, the market and the spatial index).
TIER_A_FILES: tuple[str, ...] = (
    "apps/container_logistics/assignment/app.py",
    "apps/container_logistics/assignment/accounting.py",
)
TIER_B_FILES: tuple[str, ...] = (
    "apps/container_logistics/scenario/scenario_overrides.py",
    "apps/simulation/run_container_logistics_simulation.py",
    "openride_control/policies.py",
    "openride/policy_cmd.py",
)
#: Files of a tier-A directory that are tier B instead, each with its reason. A reviewed
#: edit here is the only way out of tier A.
TIER_B_OVERRIDES: dict[str, str] = {
    f"{_POLICIES}/testing/fixtures.py": (
        "world plumbing: it BUILDS synthetic worlds (cooperation structures through "
        "datagen.hauliers, the candidate knobs of a profile); it decides nothing"),
}
# Back-compat names (read by older helpers): the globs these directories expand to.
TIER_A_GLOBS = tuple(f"{d}/**/*.py" for d, _ in TIER_A_DIRS) + TIER_A_FILES
TIER_B_GLOBS = TIER_B_FILES + tuple(TIER_B_OVERRIDES)


def is_compat(path: Path) -> bool:
    """``path`` lies in THE compat quarantine (:data:`COMPAT_PREFIX`), and nowhere else named compat."""
    rel = path.resolve().relative_to(REPO_ROOT).as_posix()
    return rel == COMPAT_PREFIX or rel.startswith(COMPAT_PREFIX + "/")


def _expand(globs: Iterable[str]) -> list[Path]:
    out: list[Path] = []
    for pattern in globs:
        out += sorted(REPO_ROOT.glob(pattern))
    return [p for p in dict.fromkeys(out)
            if not is_compat(p) and "__pycache__" not in p.relative_to(REPO_ROOT).parts]


def _tier_a_root_paths() -> list[Path]:
    """Tier A as listed: the tier directories (minus their excludes) and the single files."""
    paths: list[Path] = []
    for directory, excludes in TIER_A_DIRS:
        for path in _expand([f"{directory}/**/*.py"]):
            rel = path.relative_to(REPO_ROOT).as_posix()
            if any(rel == ex or rel.startswith(ex + "/") for ex in excludes) or rel in TIER_B_OVERRIDES:
                continue
            paths.append(path)
    return paths + _expand(TIER_A_FILES)


# --- the static import closure (G2) ----------------------------------------------------

def under(name: str, package: str) -> bool:
    return name == package or name.startswith(package + ".")


def package_of(module: str, path: Path) -> str:
    return module if path.name == "__init__.py" else module.rpartition(".")[0]


def resolve_from(node: ast.ImportFrom, package: str) -> str:
    """The absolute module of ``from <node.module> import ...`` inside ``package``."""
    if not node.level:
        return node.module or ""
    base = package.split(".")
    base = base[: len(base) - (node.level - 1)] if node.level > 1 else base
    return ".".join(base + ([node.module] if node.module else []))


def module_path(dotted_name: str) -> Path | None:
    """The source file of ``apps.*`` module ``dotted_name`` in this checkout, else ``None``."""
    if not dotted_name.startswith("apps."):
        return None
    base = REPO_ROOT.joinpath(*dotted_name.split("."))
    for candidate in (base / "__init__.py", base.with_suffix(".py")):
        if candidate.is_file():
            return candidate
    return None


def static_imports(module: str, path: Path) -> set[str]:
    """Every ``apps.*`` module ``module`` imports anywhere (local imports and parent packages included)."""
    package = package_of(module, path)
    targets: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            targets |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            base = resolve_from(node, package)
            for a in node.names:
                targets.add(f"{base}.{a.name}" if module_path(f"{base}.{a.name}") else base)
    out: set[str] = set()
    for t in targets:
        if not t.startswith("apps."):
            continue
        parts = t.split(".")
        out |= {".".join(parts[:i]) for i in range(2, len(parts) + 1)}
    return {m for m in out if module_path(m) is not None}


def _closure_stops(module: str, tier_b: set[str]) -> bool:
    return (any(under(module, pkg) for pkg in CLOSURE_STOP_PACKAGES) or module in tier_b
            or module in CLOSURE_BOUNDARY)


@functools.lru_cache(maxsize=None)
def _tier_a_closure() -> tuple[tuple[str, str, str], ...]:
    """``((module, path, first importer), ...)``: the modules the closure ADDS to the listed tier A."""
    roots = {dotted(p): p for p in _tier_a_root_paths()}
    tier_b = {dotted(p) for p in _expand(TIER_B_GLOBS)}
    seen: set[str] = set()
    added: dict[str, str] = {}
    stack = sorted(roots)
    while stack:
        module = stack.pop()
        if module in seen:
            continue
        seen.add(module)
        path = roots.get(module) or module_path(module)
        if path is None:
            continue
        for target in sorted(static_imports(module, path)):
            if target in roots or target in added or _closure_stops(target, tier_b):
                continue
            added[target] = module
            stack.append(target)
    return tuple((m, str(module_path(m)), importer) for m, importer in sorted(added.items()))


def closure_modules() -> dict[str, str]:
    """``{module: first importer}`` for every module tier A gains by its static import closure."""
    return {m: importer for m, _path, importer in _tier_a_closure()}


def _tier_a_paths() -> list[Path]:
    return _tier_a_root_paths() + [Path(path) for _m, path, _i in _tier_a_closure()]


def dotted(path: Path) -> str:
    """``apps/x/y.py`` -> ``apps.x.y``; a package ``__init__`` is the package itself."""
    parts = list(path.relative_to(REPO_ROOT).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def tier_a_modules() -> dict[str, Path]:
    return {dotted(p): p for p in _tier_a_paths()}


def tier_b_modules() -> dict[str, Path]:
    return {dotted(p): p for p in _expand(TIER_B_GLOBS)}


# --- baselines -------------------------------------------------------------------------

_TAG = re.compile(r"#\s*removed-by:\s*(WS\w+)\s*$")


@dataclass(frozen=True)
class BaselineLine:
    token: str
    count: int
    removed_by: str | None


def read_baseline(path: Path) -> dict[str, BaselineLine]:
    lines: dict[str, BaselineLine] = {}
    if not path.exists():
        return lines
    for n, raw in enumerate(path.read_text().splitlines(), 1):
        if not raw.strip():
            continue
        fields = raw.split("\t")
        if len(fields) not in (2, 3) or not fields[1].isdigit():
            raise ValueError(f"{path}:{n}: expected 'token<TAB>count[<TAB># removed-by: WS<n>]', got {raw!r}")
        tag = None
        if len(fields) == 3:
            m = _TAG.fullmatch(fields[2].strip())
            if not m:
                raise ValueError(f"{path}:{n}: bad tag {fields[2]!r} (want '# removed-by: WS<n>')")
            tag = m.group(1)
        if fields[0] in lines:
            raise ValueError(f"{path}:{n}: duplicate token {fields[0]!r}")
        lines[fields[0]] = BaselineLine(fields[0], int(fields[1]), tag)
    return lines


def compare(module: str, actual: Mapping[str, int],
            baseline: Mapping[str, BaselineLine] | None = None) -> list[str]:
    """Every problem of ``module``'s scan (empty means clean).

    ``baseline`` is a re-created baseline file's lines, if any (``read_baseline``); with
    :data:`REQUIRE_EMPTY_BASELINES` each of its lines is itself a problem, so it can not
    allow a hit.
    """
    baseline = baseline or {}
    problems: list[str] = []
    for tok in sorted(actual):
        allowed = baseline[tok].count if tok in baseline else 0
        if actual[tok] > allowed:
            problems.append(f"{module}: {tok} occurs {actual[tok]}x, baseline allows {allowed} (new framework "
                            "vocabulary: move it to the solver side or policies/compat/)")
    for tok, line in sorted(baseline.items()):
        have = actual.get(tok, 0)
        if line.count > have:
            problems.append(f"{module}: baseline says {tok} {line.count}x, scan finds {have}")
        if line.removed_by and line.removed_by in MERGED_WORKSTREAMS:
            problems.append(f"{module}: {tok} is tagged '# removed-by: {line.removed_by}', and {line.removed_by} "
                            "has merged; remove the hit and the line")
        if REQUIRE_EMPTY_BASELINES:
            problems.append(f"{module}: baseline line {tok} exists; the guard has no baselines since WS8 "
                            "(remove the hit, do not baseline it)")
    return problems
