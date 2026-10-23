"""Framework-vocabulary guard: the framework may not grow solver vocabulary.

Plan ``plan_contract_final.md`` §7.3 (WS0), H2/H4. The generic solver contract means
the framework side names no concrete solver and none of their concepts; those live with
the solvers (``dispatch/library/``, ``solver_lib/``, ``assignment/solver/``, the market
modules) or, for old inputs and wire formats, in ``policies/compat/`` (exempt).

What is scanned (``ast``, so comments and docstrings are skipped): identifiers --
``Name.id``, ``Attribute.attr``, def/class names, args, keyword names, import modules and
aliases, ``except ... as``, ``global``/``nonlocal`` -- and every non-docstring ``str``
constant (f-string parts included). Each is cut into words on non-word characters; each
word is checked whole against the banned identifiers, then split on ``_`` and camel case
(``PooledMarket`` -> ``pooled``, ``market``), lower-cased, and checked against the banned
subwords. Hits are counted per token as ``sub:<w>``, ``pair:component_of``, ``id:<name>``
or ``str:v1``. END STATE (WS8): every guarded module has ZERO hits -- the per-module
baselines WS0 started with (``tests/vocabulary_baseline/``, shrink-only, with
``# removed-by:`` tags for transitional lines) are deleted, and the only exemption is
``policies/compat/**`` (see ``tests/_framework_guard.py``).

Tier A (core runtime) and tier B (world plumbing) are listed in ``_framework_guard``.
``pool`` and ``partition`` are deliberately NOT banned subwords: they would hit
``ThreadPoolExecutor`` / ``_eventlet_tpool`` and ``str.partition``. Their real cases are
banned whole identifiers instead (``pool_id``, ``PartitionRule``, ...).

Verified to have teeth (WS0, mutate / see it fail / restore):

- ``if top.name == "PooledMarket":`` added to ``assignment/app.py`` failed
  (``sub:pooled`` and ``sub:market`` above baseline -- the camel-case split);
- ``market_notes = 1`` added to ``app.py`` failed (``sub:market``);
- lowering a baseline count by one failed (hit above baseline); raising one failed ("the
  baseline only shrinks", and ``--shrink`` restored it);
- a ``# removed-by: WS0`` tag on a baseline line failed (WS0 is merged);
- a baseline file for a module that is not guarded failed (orphan).

Verified to have teeth (WS8, end state; mutate / see it fail / restore):

- ``x = "market"`` added to ``dispatch/runner.py`` failed
  ``test_module_has_no_solver_vocabulary`` (``sub:market`` occurs 1x, allows 0);
- re-creating ``tests/vocabulary_baseline/apps.container_logistics.policies.dispatch.runner.txt``
  with the line ``sub:market<TAB>1`` (the WS0 way of allowing that hit) still failed it
  (the line itself is reported), and failed ``test_no_baseline_directory_exists``.
"""

from __future__ import annotations

import ast
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Iterator

import pytest

try:
    from tests import _framework_guard as guard
except ImportError:  # run as a script from the repo root
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from tests import _framework_guard as guard

BANNED_SUBWORDS = frozenset({
    "pooled", "market", "partitioned", "greedy", "topology",
    "pooledmarket", "greedynearest", "randomassignment",
})
BANNED_SUBWORDS_TIER_A = frozenset({"cooperation", "partners"})
BANNED_SUBWORD_PAIRS_TIER_A = frozenset({("component", "of")})
BANNED_IDENTIFIERS = frozenset("""
    pool_id market_round PartitionRule CompanyRule ScopeDecider INFORMATION_MODELS
    information_model per_group per_company SHARE_TAG_FORMATS share_tag_format
    V1_SHARE_TAG_BUILTINS PAIR_NOTE_KEYS round_stats candidate_kind candidate_kind_for
    KIND_WITHOUT_CANDIDATES_PARAM CANDIDATE_KINDS TICK_SAMPLE_SCOPE sample_scope_for
    wrap_legacy_leaf BUILTIN_COMPONENT_MODULES accounting_params_for _ACCOUNTING_KEYS
    dual_cycle_bonus_km dual_cycle_radius_km pools_from_structure PoolMarket
    structure_allows may_serve cooperation_pairs proves_physical tick_allowed _v1_tag
    partition_context company_context partition_scope_key company_scope_key
""".split())
BANNED_IDENTIFIERS_TIER_A = frozenset({"active_cooperation_structure", "share_adjacency", "share_eligible"})
BANNED_STRINGS_TIER_A = frozenset({"v1"})

BASELINE_DIR = guard.BASELINE_DIR

_WORD = re.compile(r"\w+")
_CAMEL = re.compile(r"[A-Z]+(?=[A-Z][a-z]|\d|$)|[A-Z]?[a-z]+|[A-Z]+|\d+")


def subwords(word: str) -> list[str]:
    """``PooledMarket`` -> ``[pooled, market]``; ``V1_SHARE`` -> ``[v1, share]``; ``tpool`` -> ``[tpool]``."""
    out: list[str] = []
    for part in word.split("_"):
        if not part:
            continue
        pieces = _CAMEL.findall(part)
        # Keep a letter+digit run together (``v1``, ``h3``), as written.
        merged: list[str] = []
        for p in pieces:
            if p.isdigit() and merged and not merged[-1].isdigit():
                merged[-1] += p
            else:
                merged.append(p)
        out += [p.lower() for p in merged]
    return out


def _docstring_ids(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                ids.add(id(body[0].value))
    return ids


def _texts(tree: ast.AST) -> Iterator[tuple[str, str]]:
    """``("ident" | "str", text)`` for every scanned identifier and non-docstring string."""
    docstrings = _docstring_ids(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            yield "ident", node.id
        elif isinstance(node, ast.Attribute):
            yield "ident", node.attr
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            yield "ident", node.name
        elif isinstance(node, ast.arg):
            yield "ident", node.arg
        elif isinstance(node, ast.keyword) and node.arg:
            yield "ident", node.arg
        elif isinstance(node, ast.alias):
            yield "ident", node.name
            if node.asname:
                yield "ident", node.asname
        elif isinstance(node, ast.ImportFrom) and node.module:
            yield "ident", node.module
        elif isinstance(node, ast.ExceptHandler) and node.name:
            yield "ident", node.name
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            for name in node.names:
                yield "ident", name
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            yield "str", node.value


def scan_source(source: str, *, tier_a: bool) -> Counter:
    """Banned-vocabulary hits of one module's source, as ``Counter({token: count})``."""
    subs = BANNED_SUBWORDS | (BANNED_SUBWORDS_TIER_A if tier_a else frozenset())
    pairs = BANNED_SUBWORD_PAIRS_TIER_A if tier_a else frozenset()
    idents = BANNED_IDENTIFIERS | (BANNED_IDENTIFIERS_TIER_A if tier_a else frozenset())
    hits: Counter = Counter()
    for kind, text in _texts(ast.parse(source)):
        if kind == "str" and tier_a and text in BANNED_STRINGS_TIER_A:
            hits[f"str:{text}"] += 1
        for word in _WORD.findall(text):
            if word in idents:
                hits[f"id:{word}"] += 1
            parts = subwords(word)
            for p in parts:
                if p in subs:
                    hits[f"sub:{p}"] += 1
            for a, b in zip(parts, parts[1:]):
                if (a, b) in pairs:
                    hits[f"pair:{a}_{b}"] += 1
    return hits


def scan_all() -> dict[str, Counter]:
    out: dict[str, Counter] = {}
    for module, path in guard.tier_a_modules().items():
        out[module] = scan_source(path.read_text(), tier_a=True)
    for module, path in guard.tier_b_modules().items():
        out[module] = scan_source(path.read_text(), tier_a=False)
    return out


_SCANS = scan_all()


def test_the_tiers_name_real_modules():
    a, b = guard.tier_a_modules(), guard.tier_b_modules()
    assert "apps.container_logistics.assignment.app" in a
    assert "apps.container_logistics.policies.dispatch.runner" in a
    assert not any(".library." in m or ".compat" in m for m in a), "library/ and compat/ are not tier A"
    assert len(b) == len(guard.TIER_B_GLOBS), sorted(b)
    assert not set(a) & set(b)


@pytest.mark.parametrize("module", sorted(_SCANS))
def test_module_has_no_solver_vocabulary(module):
    problems = guard.compare(module, _SCANS[module], guard.read_baseline(BASELINE_DIR / f"{module}.txt"))
    assert not problems, "\n".join(problems)


def test_no_baseline_directory_exists():
    """The end state has no baselines (WS8): a hit is fixed, never baselined."""
    assert guard.REQUIRE_EMPTY_BASELINES is True
    assert "WS8" in guard.MERGED_WORKSTREAMS
    assert not BASELINE_DIR.exists(), f"{BASELINE_DIR} was re-created; the guard allows no baseline"


def test_a_recreated_baseline_line_can_not_allow_a_hit(tmp_path):
    """Writing a WS0-style baseline line (tagged or not) never makes a hit pass."""
    path = tmp_path / "m.txt"
    path.write_text("sub:market\t1\nsub:pooled\t1\t# removed-by: WS8\n")
    problems = guard.compare("m", {"sub:market": 1, "sub:pooled": 1}, guard.read_baseline(path))
    assert any("sub:market" in p and "no baselines since WS8" in p for p in problems), problems
    assert any("removed-by: WS8" in p for p in problems), problems
    assert guard.compare("m", {"sub:market": 1}) == [
        "m: sub:market occurs 1x, baseline allows 0 (new framework vocabulary: move it to the "
        "solver side or policies/compat/)"]
    assert guard.compare("m", {}) == []


@pytest.mark.parametrize(("source", "tier_a", "want"), [
    ('if top.name == "PooledMarket": pass', True, {"sub:pooled": 1, "sub:market": 1}),
    ("market_notes = 1", True, {"sub:market": 1}),
    ("x = s.partition('.')", True, {}),
    ("from concurrent.futures import ThreadPoolExecutor\nfrom eventlet import tpool as _eventlet_tpool", True, {}),
    ("component_of = {}", True, {"pair:component_of": 1}),
    ("component_of = {}", False, {}),
    ('msg = f"pool_id={x}"', False, {"id:pool_id": 1}),
    ('fmt = "v1"', True, {"str:v1": 1}),
    ('fmt = "v1"', False, {}),
    ('"""A pooled market docstring."""\n# pooled comment\nx = 1', True, {}),
    ("def f():\n    '''pooled'''\n    return 'pooled'", True, {"sub:pooled": 1}),
    ("DISPATCH_GREEDY_NEAREST = 1", False, {"sub:greedy": 1}),
    ("from apps.container_logistics.assignment.pooled_planner import x", False, {"sub:pooled": 1}),
])
def test_the_scanner_tokenises_as_specified(source, tier_a, want):
    assert dict(scan_source(source, tier_a=tier_a)) == want
