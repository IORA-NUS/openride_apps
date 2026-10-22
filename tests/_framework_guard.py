"""Shared machinery of the framework-vocabulary and framework-import guards.

Plan ``plan_contract_final.md`` §7.3 (WS0). Two tests use it:

- ``tests/test_framework_vocabulary.py`` -- the guarded modules may not grow solver
  vocabulary (``pooled``, ``market``, ``PartitionRule``, ...).
- ``tests/test_framework_imports.py`` -- tier A may not import solver packages, may take
  only allow-listed symbols from ``policies.compat``, and may call only allow-listed
  ``assignment.constraints`` functions.

Both compare a per-module scan with a per-module BASELINE file of sorted
``token<TAB>count`` lines (optionally ``<TAB># removed-by: WS<n>``). A baseline can only
SHRINK: the test fails on any hit above its baseline count (or absent from it), and on
any baseline count above the actual count, so a fix must also lower the baseline. The
end state (WS8) is every baseline empty.

Transitional additions (plan H4): a workstream may ADD a baseline line only with a
``# removed-by: WS<n>`` tag naming a LATER workstream. When WS<n> merges it adds its id
to :data:`MERGED_WORKSTREAMS`, and a line still tagged with a merged id fails the guard.
WS8 sets :data:`REQUIRE_EMPTY_BASELINES`.

Maintenance CLI (run from the repo root)::

    python -m tests.test_framework_vocabulary --shrink   # lower counts / drop lines only
    python -m tests.test_framework_imports --shrink
    python -m tests.test_framework_vocabulary --write    # WS0 ONLY: rewrite from the scan

``--shrink`` never adds a line or raises a count, so it can not launder a new hit.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Mapping

REPO_ROOT = Path(__file__).resolve().parents[1]
BASELINE_DIR = REPO_ROOT / "tests" / "vocabulary_baseline"

# Workstreams that have merged into feat/policy-slots. A baseline line tagged
# ``# removed-by: WS<n>`` for a merged WS<n> fails the guard. Each WS adds its own id here.
MERGED_WORKSTREAMS: frozenset[str] = frozenset({"WS0", "WS1", "WS2", "WS3", "WS4a", "WS5"})
# WS8 flips this: every baseline must then be empty (the framework names no solver).
REQUIRE_EMPTY_BASELINES = False

_POLICIES = "apps/container_logistics/policies"

# Tier A: core runtime. Tier B: world plumbing. Globs relative to the repo root,
# top level only (``dispatch/*.py`` does NOT include ``dispatch/library/``).
TIER_A_GLOBS = (
    f"{_POLICIES}/framework/*.py",
    f"{_POLICIES}/dispatch/*.py",
    f"{_POLICIES}/testing/battery.py",
    f"{_POLICIES}/testing/bench.py",
    "apps/container_logistics/assignment/app.py",
    "apps/container_logistics/assignment/accounting.py",
)
TIER_B_GLOBS = (
    "apps/container_logistics/scenario/scenario_overrides.py",
    "apps/simulation/run_container_logistics_simulation.py",
    "openride_control/policies.py",
    "openride/policy_cmd.py",
)


def _expand(globs: Iterable[str]) -> list[Path]:
    out: list[Path] = []
    for pattern in globs:
        out += sorted(REPO_ROOT.glob(pattern))
    return [p for p in dict.fromkeys(out) if "/compat/" not in p.as_posix()]


def dotted(path: Path) -> str:
    """``apps/x/y.py`` -> ``apps.x.y``; a package ``__init__`` is the package itself."""
    parts = list(path.relative_to(REPO_ROOT).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def tier_a_modules() -> dict[str, Path]:
    return {dotted(p): p for p in _expand(TIER_A_GLOBS)}


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


def write_baseline(path: Path, counts: Mapping[str, int], tags: Mapping[str, str] | None = None) -> None:
    tags = tags or {}
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "".join(
        f"{tok}\t{counts[tok]}" + (f"\t# removed-by: {tags[tok]}" if tok in tags else "") + "\n"
        for tok in sorted(counts) if counts[tok] > 0
    )
    path.write_text(body)


def compare(module: str, actual: Mapping[str, int], baseline: Mapping[str, BaselineLine],
            shrink_cmd: str) -> list[str]:
    """Every problem of ``module``'s scan against its baseline (empty means clean)."""
    problems: list[str] = []
    for tok in sorted(actual):
        allowed = baseline[tok].count if tok in baseline else 0
        if actual[tok] > allowed:
            problems.append(f"{module}: {tok} occurs {actual[tok]}x, baseline allows {allowed} (new framework "
                            "vocabulary: move it to the solver side or policies/compat/)")
    for tok, line in sorted(baseline.items()):
        have = actual.get(tok, 0)
        if line.count > have:
            problems.append(f"{module}: baseline says {tok} {line.count}x, scan finds {have}; the baseline only "
                            f"shrinks -- run `{shrink_cmd}`")
        if line.removed_by and line.removed_by in MERGED_WORKSTREAMS:
            problems.append(f"{module}: {tok} is tagged '# removed-by: {line.removed_by}', and {line.removed_by} "
                            "has merged; remove the hit and the line")
        if REQUIRE_EMPTY_BASELINES:
            problems.append(f"{module}: baseline line {tok} remains; WS8 requires every baseline empty")
    return problems


def orphan_baselines(directory: Path, modules: Iterable[str]) -> list[str]:
    """Baseline files naming a module that is no longer guarded (deleted or renamed)."""
    known = set(modules)
    return [f"{p}: no guarded module {p.stem!r}; delete the file" for p in sorted(directory.glob("*.txt"))
            if p.stem not in known]


def shrink(directory: Path, scans: Mapping[str, Mapping[str, int]]) -> int:
    """Lower each baseline count to the scan (never raise one, never add a line). Returns edits."""
    edits = 0
    for module, actual in scans.items():
        path = directory / f"{module}.txt"
        base = read_baseline(path)
        counts = {tok: min(line.count, actual.get(tok, 0)) for tok, line in base.items()}
        tags = {tok: line.removed_by for tok, line in base.items() if line.removed_by}
        if any(counts[t] != base[t].count for t in base):
            edits += 1
            write_baseline(path, counts, tags)
    return edits


def main(directory: Path, scan_all: Callable[[], Mapping[str, Mapping[str, int]]], argv: list[str]) -> int:
    """``--shrink`` (safe) or ``--write`` (WS0 only: rewrite every baseline from the scan)."""
    scans = scan_all()
    if argv == ["--shrink"]:
        print(f"shrunk {shrink(directory, scans)} baseline file(s) in {directory}")
        return 0
    if argv == ["--write"]:
        directory.mkdir(parents=True, exist_ok=True)
        for module, actual in scans.items():
            path = directory / f"{module}.txt"
            tags = {t: line.removed_by for t, line in read_baseline(path).items() if line.removed_by}
            write_baseline(path, actual, {t: w for t, w in tags.items() if t in actual})
        print(f"wrote {len(scans)} baseline file(s) in {directory}; REVIEW every line")
        return 0
    print("usage: --shrink | --write", file=sys.stderr)
    return 2
