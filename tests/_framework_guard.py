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

Re-creating a baseline file can not launder a hit: :func:`compare` reports every line
of one (``REQUIRE_EMPTY_BASELINES``), and every ``# removed-by:`` tag names a merged
workstream (:data:`MERGED_WORKSTREAMS`), so it is reported as stale too.
"""

from __future__ import annotations

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


def _expand(globs: Iterable[str]) -> list[Path]:
    out: list[Path] = []
    for pattern in globs:
        out += sorted(REPO_ROOT.glob(pattern))
    return [p for p in dict.fromkeys(out)
            if "/compat/" not in p.as_posix() and "__pycache__" not in p.as_posix()]


def _tier_a_paths() -> list[Path]:
    paths: list[Path] = []
    for directory, excludes in TIER_A_DIRS:
        for path in _expand([f"{directory}/**/*.py"]):
            rel = path.relative_to(REPO_ROOT).as_posix()
            if any(rel == ex or rel.startswith(ex + "/") for ex in excludes) or rel in TIER_B_OVERRIDES:
                continue
            paths.append(path)
    return paths + _expand(TIER_A_FILES)


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
