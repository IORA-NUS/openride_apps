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

REGISTRY VOCABULARY (final review H3). The static lists below are blind to a name whose
camel-case parts are innocent (``RandomAssignment`` -> ``random`` + ``assignment``), and
to the built-ins' PARAM names (``p["candidates"]``, ``max_rounds``, ``inner``). So the
banned set is also BUILT FROM THE REGISTRY at test time (:func:`registry_vocabulary`):
every registered policy name and component name (``name:<X>``: case-sensitive, inside
any identifier or non-docstring string, not followed by a lower-case letter or digit, so
``RandomAssignmentSolver`` and ``"PooledMarket(offer=OfferNone)"`` hit and ``OfferAllowed``
does not), every declared param name of every policy and component (``param:<p>``: an
identifier equal to it; in a string, a compound name as any whole word, a plain-word
name only as the whole string or in key/path syntax, :func:`_param_pattern`), and every component kind name
(``kind:<k>``, inside a string). A solver added tomorrow extends the guard by
registering. Two framework-owned spellings are exempt, each pinned by a test:
:data:`FRAMEWORK_OWNED_PARAMS` (``inner``, the framework's own descent param,
``registry.INNER_PARAM``) everywhere, and :data:`FRAMEWORK_SURFACE_IDENTIFIERS`
(``candidates``, ``Tick.candidates`` / ``Tick.restrict(candidates=...)``) in identifier
positions only -- the STRING ``"candidates"`` (a param read) is still banned. A new
solver param that collides with a framework identifier fails here; rename the param or
add a reviewed entry with its reason.

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

Verified to have teeth (FB, final review H3; mutate / see it fail / restore):

- the reviewer's mutation in ``assignment/app.py::publish`` (``if
  runner.tree.top.name == "RandomAssignment": assignment = assignment[1:]``), which the
  old scan returned ``{}`` for, failed ``test_module_has_no_solver_vocabulary[...app]``
  (``name:RandomAssignment`` occurs 1x);
- ``if p["candidates"] == "spatial":`` re-added to ``dispatch/base.py`` failed it
  (``param:candidates``), and ``max_rounds = 3`` in ``dispatch/runner.py`` failed it
  (``param:max_rounds``);
- renaming the ``FRAMEWORK_SURFACE_IDENTIFIERS`` entry away failed seven modules (every
  ``tick.candidates`` / ``restrict(candidates=...)`` / assignable-``candidates`` read)
  plus the two exemption tests, i.e. the exemption is load-bearing and exactly scoped.

Round 2 (fix coder R3B) closed three holes, each verified to have teeth in a private
copy of the tree (mutate / see it fail / restore; at ``6647aaf`` every one of these
mutations left the 125 guard tests green):

- G1, the compat exemption is ONE prefix (``_framework_guard.COMPAT_PREFIX``): a new
  ``policies/dispatch/compat/branch.py`` with ``top.name == "RandomAssignment"``,
  imported by ``runner.py``, failed ``test_module_has_no_solver_vocabulary[...dispatch.compat.branch]``;
  ``policies/framework/compat/__init__.py`` importing ``solver_lib`` failed the vocabulary
  test, ``test_tier_a_imports_obey_the_rules`` and the transitive-gateway test;
- G2, tier A includes its static import closure (``_framework_guard.CLOSURE_BOUNDARY``
  is the only reviewed stop besides compat, the solver packages and tier B): a new
  ``assignment/publish_helpers.py`` with the same name branch, imported by ``app.py``,
  failed ``test_module_has_no_solver_vocabulary[...publish_helpers]``; with a local
  ``compat.legacy.wrap_legacy_leaf`` import instead it failed that and
  ``test_tier_a_imports_obey_the_rules[...publish_helpers]``;
- the ``candidates`` gap (:func:`surface_comparisons`): ``params.candidates == "spatial"``
  appended to ``dispatch/base.py`` failed ``test_module_has_no_solver_vocabulary[...dispatch.base]``
  (``cmp:candidates``).

The closure reaches ``assignment/agent.py`` (the ORSim agent shell, via the package
``__init__``), whose ``entering_market`` / ``exiting_market`` are the ENGINE's hook names:
:data:`ENGINE_HOOK_IDENTIFIERS` exempts exactly those two, in identifier positions only,
pinned to ``orsim.lifecycle.ORSimAgent``.
"""

from __future__ import annotations

import ast
import dataclasses
import re
import sys
from collections import Counter
from dataclasses import dataclass
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

#: Registered names the registry may be unable to report (discovery broken): the guard
#: still bans them. The live set comes from :func:`registry_vocabulary`.
STATIC_POLICY_NAMES = frozenset({"GreedyNearest", "RandomAssignment", "Partitioned", "PooledMarket"})
#: Param names the FRAMEWORK owns (a solver adopts them, not the other way round):
#: exempt everywhere. ``inner`` is ``registry.INNER_PARAM``, the param through which the
#: framework descends a selection tree (``PolicySpec.inner_param``).
FRAMEWORK_OWNED_PARAMS: dict[str, str] = {
    "inner": "registry.INNER_PARAM: the framework's own selection-tree descent param",
}
#: Param names that are ALSO a framework identifier: exempt in identifier positions only
#: (a string literal -- ``p["candidates"]``, ``getattr(params, "candidates")`` -- still hits).
FRAMEWORK_SURFACE_IDENTIFIERS: dict[str, str] = {
    "candidates": ("Tick.candidates / Tick.restrict(candidates=...): the caller-built pairs of a "
                   "sub-tick; and the app's assignable-truck list. Not a read of any solver's param"),
}

#: ORSim's agent lifecycle hooks (``orsim.lifecycle.ORSimAgent``): ENGINE vocabulary that
#: a closure module (``assignment/agent.py``, the agent shell) must spell to override them.
#: Exempt in identifier positions only; a string still hits. Pinned to the engine's API.
ENGINE_HOOK_IDENTIFIERS: dict[str, str] = {
    "entering_market": "orsim.lifecycle.ORSimAgent.entering_market: the engine's agent-activation hook",
    "exiting_market": "orsim.lifecycle.ORSimAgent.exiting_market: the engine's agent-exit hook",
}


@dataclass(frozen=True)
class Vocabulary:
    """The registry-derived banned vocabulary (:func:`registry_vocabulary`)."""

    names: frozenset[str]           # policy names and component names
    params: frozenset[str]          # declared param names (framework-owned ones removed)
    kinds: frozenset[str]           # component kind names

    @property
    def name_pattern(self) -> re.Pattern | None:
        if not self.names:
            return None
        alts = "|".join(re.escape(n) for n in sorted(self.names, key=len, reverse=True))
        return re.compile(rf"({alts})(?![a-z0-9])")


EMPTY_VOCABULARY = Vocabulary(frozenset(), frozenset(), frozenset())


def registry_vocabulary() -> Vocabulary:
    """Every registered policy / component name, param name and component kind (built-ins discovered)."""
    from apps.container_logistics.policies.framework import components as _components
    from apps.container_logistics.policies.framework.params import declared_params
    from apps.container_logistics.policies.framework.registry import discover_builtin_policies, list_policies

    discover_builtin_policies()
    names: set[str] = set(STATIC_POLICY_NAMES)
    params: set[str] = set()
    kinds: set[str] = set()
    for spec in list_policies():
        names.add(spec.name)
        params |= {info.name for info in declared_params(spec.params_cls)}
    for kind, comp in _components._KINDS.items():
        kinds.add(kind)
        for name, cls in comp.registry_getter().items():
            names.add(name)
            params_cls = getattr(cls, "Params", None)
            if isinstance(params_cls, type) and dataclasses.is_dataclass(params_cls):
                params |= {f.name for f in dataclasses.fields(params_cls)}
    return Vocabulary(names=frozenset(names), params=frozenset(params) - set(FRAMEWORK_OWNED_PARAMS),
                      kinds=frozenset(kinds))


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


def _param_pattern(name: str) -> re.Pattern:
    """Where a param name counts inside a STRING.

    A compound name (``dual_cycle_bonus_km``, ``max_rounds``) is never prose: any whole
    word hits. A plain word (``claim``, ``offer``, ``candidates``) is also English, so it
    hits only as the whole string or in key/path syntax: after ``.`` or ``[``, or before
    ``=``, ``]`` or ``.<word>`` (``"planner.inner.candidates"``, ``"candidates=spatial"``,
    ``p["claim"]`` is the whole-string case). Prose such as "the claim is unbacked" does not.
    """
    word = re.escape(name)
    if "_" in name:
        return re.compile(rf"(?<!\w){word}(?!\w)")
    return re.compile(rf"^{word}$|(?<=[.\[]){word}(?!\w)|(?<!\w){word}(?=\s*[=\]]|\.\w)")


def _str_literals(node: ast.AST) -> list[str]:
    """The string literal(s) ``node`` is: a ``str`` constant, or a tuple / list / set of them."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return [e.value for e in node.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)]
    return []


def _surface_name(node: ast.AST) -> str | None:
    """``x.candidates`` / ``candidates`` -> ``candidates`` (a framework-surface identifier), else ``None``."""
    name = node.attr if isinstance(node, ast.Attribute) else node.id if isinstance(node, ast.Name) else None
    return name if name in FRAMEWORK_SURFACE_IDENTIFIERS else None


def surface_comparisons(tree: ast.AST) -> Counter:
    """A framework-surface identifier COMPARED with a string literal (round 2, candidates gap).

    The ``candidates`` exemption covers ``Tick.candidates`` / ``restrict(candidates=...)``,
    which hold pairs, never a mode string; so ``params.candidates == "spatial"``,
    ``"sampled" != candidates``, ``candidates in ("spatial", ...)`` and ``match
    params.candidates: case "spatial":`` are a read of a SOLVER param under the exempt
    spelling. Each is a ``cmp:<identifier>`` hit (``policies/compat/`` is never scanned).
    """
    hits: Counter = Counter()
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            operands = [node.left, *node.comparators]
            names = [n for n in map(_surface_name, operands) if n]
            if names and any(_str_literals(o) for o in operands):
                hits.update(f"cmp:{n}" for n in names)
        elif isinstance(node, ast.Match) and _surface_name(node.subject):
            if any(isinstance(sub, ast.MatchValue) and _str_literals(sub.value)
                   for case in node.cases for sub in ast.walk(case.pattern)):
                hits[f"cmp:{_surface_name(node.subject)}"] += 1
    return hits


def scan_source(source: str, *, tier_a: bool, vocab: Vocabulary | None = None) -> Counter:
    """Banned-vocabulary hits of one module's source, as ``Counter({token: count})``.

    ``vocab`` defaults to the registry's (:data:`VOCAB`); pass :data:`EMPTY_VOCABULARY`
    for the static lists alone.
    """
    vocab = VOCAB if vocab is None else vocab
    subs = BANNED_SUBWORDS | (BANNED_SUBWORDS_TIER_A if tier_a else frozenset())
    pairs = BANNED_SUBWORD_PAIRS_TIER_A if tier_a else frozenset()
    idents = BANNED_IDENTIFIERS | (BANNED_IDENTIFIERS_TIER_A if tier_a else frozenset())
    name_re = vocab.name_pattern
    param_res = {p: _param_pattern(p) for p in vocab.params}
    tree = ast.parse(source)
    hits: Counter = surface_comparisons(tree)
    for kind, text in _texts(tree):
        if kind == "ident" and text in ENGINE_HOOK_IDENTIFIERS:
            continue
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
        if name_re is not None:
            for m in name_re.finditer(text):
                hits[f"name:{m.group(1)}"] += 1
        if kind == "ident":
            if text in vocab.params and text not in FRAMEWORK_SURFACE_IDENTIFIERS:
                hits[f"param:{text}"] += 1
        else:
            for p, rx in param_res.items():
                n = len(rx.findall(text))
                if n:
                    hits[f"param:{p}"] += n
            for k in vocab.kinds:
                if k in text:
                    hits[f"kind:{k}"] += text.count(k)
    return hits


def scan_all() -> dict[str, Counter]:
    out: dict[str, Counter] = {}
    for module, path in guard.tier_a_modules().items():
        out[module] = scan_source(path.read_text(), tier_a=True)
    for module, path in guard.tier_b_modules().items():
        out[module] = scan_source(path.read_text(), tier_a=False)
    return out


VOCAB = registry_vocabulary()
_SCANS = scan_all()


def test_the_tiers_name_real_modules():
    a, b = guard.tier_a_modules(), guard.tier_b_modules()
    assert "apps.container_logistics.assignment.app" in a
    assert "apps.container_logistics.policies.dispatch.runner" in a
    assert not any(guard.under(m, pkg) for m in a for pkg in guard.CLOSURE_STOP_PACKAGES), \
        "the compat quarantine and the solver packages (library/ included) are not tier A"
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
    assert dict(scan_source(source, tier_a=tier_a, vocab=EMPTY_VOCABULARY)) == want


@pytest.mark.parametrize(("source", "want"), [
    # The review's blind spots (H3): each scanned to {} with the static lists alone.
    ('if spec.name == "RandomAssignment": pass', {"name:RandomAssignment": 1}),
    ("if p['candidates'] == 'spatial': pass", {"param:candidates": 1}),
    ("x = getattr(params, 'candidates', None)", {"param:candidates": 1}),
    ("max_rounds = 3", {"param:max_rounds": 1}),
    ("f(max_rounds=3)", {"param:max_rounds": 1}),
    ("cls = RandomAssignmentSolver", {"name:RandomAssignment": 1}),
    ('label = "Partitioned/GreedyNearest"', {"name:Partitioned": 1, "name:GreedyNearest": 1,
                                             "sub:partitioned": 1, "sub:greedy": 1}),
    ('msg = f"use {x}.dual_cycle_bonus_km"', {"param:dual_cycle_bonus_km": 1, "id:dual_cycle_bonus_km": 1}),
    ('arb = "OwnerFirst"', {"name:OwnerFirst": 1}),
    ('k = "market.claim"', {"kind:market.claim": 1, "sub:market": 1, "param:claim": 1}),
    ('msg = "the claim is unbacked; an offer"', {}),
    ('key = "planner.inner.claim"', {"param:claim": 1}),
    ('arg = "candidates=spatial"', {"param:candidates": 1}),
    # Case-sensitive, whole names: a lower-case word or a longer camel word is not a name.
    ('x = "random assignment"', {}),
    ("OfferAllowed = 1", {}),
    # The framework-owned spellings.
    ("sub = tick.restrict(trucks=t, orders=o, scope='s', sample_rng=None, candidates=None)", {}),
    ("x = node.inner\ny = 'inner'", {}),
])
def test_the_scanner_bans_the_registry_vocabulary(source, want):
    assert dict(scan_source(source, tier_a=False)) == want


def test_the_registry_vocabulary_is_the_registry():
    """The banned set really is read from the registry (not a copy), and covers the review's words."""
    assert {"GreedyNearest", "RandomAssignment", "Partitioned", "PooledMarket"} <= VOCAB.names
    assert {"OfferSpare", "ClaimIfGainExceeds", "LowestCost"} <= VOCAB.names, "component names"
    assert {"candidates", "max_rounds", "dual_cycle_bonus_km", "min_gain_km"} <= VOCAB.params
    assert {"market.offer", "market.claim", "market.arbitration"} <= VOCAB.kinds
    assert not set(FRAMEWORK_OWNED_PARAMS) & VOCAB.params


def test_the_framework_owned_exemptions_are_real_and_not_stale():
    """Each exemption names a framework-owned spelling AND a param some solver declares."""
    import inspect

    from apps.container_logistics.policies.dispatch.tick import Tick
    from apps.container_logistics.policies.framework import registry

    assert set(FRAMEWORK_OWNED_PARAMS) == {registry.INNER_PARAM}
    assert "candidates" in inspect.signature(Tick.restrict).parameters
    assert set(FRAMEWORK_SURFACE_IDENTIFIERS) == {"candidates"}
    declared = registry_vocabulary().params | set(FRAMEWORK_OWNED_PARAMS)
    raw = set()
    for spec in registry.list_policies():
        from apps.container_logistics.policies.framework.params import declared_params

        raw |= {i.name for i in declared_params(spec.params_cls)}
    assert set(FRAMEWORK_OWNED_PARAMS) <= raw and set(FRAMEWORK_SURFACE_IDENTIFIERS) <= declared, \
        "an exemption for a param no solver declares any more is stale: remove it"


# --- round 2: the candidates gap, the compat prefix (G1), the tier-A closure (G2) -----

@pytest.mark.parametrize(("source", "want"), [
    ('if params.candidates == "spatial": pass', {"cmp:candidates": 1}),
    ('if "sampled" != tick.candidates: pass', {"cmp:candidates": 1}),
    ('ok = candidates in ("spatial", "sampled")', {"cmp:candidates": 1}),
    ('match p.candidates:\n    case "spatial":\n        pass', {"cmp:candidates": 1}),
    # The framework surface itself: pairs, never a mode string.
    ("if tick.candidates is None: pass", {}),
    ("same = sub.candidates == tick.candidates", {}),
    ("sub = tick.restrict(trucks=t, orders=o, scope='s', sample_rng=None, candidates=None)", {}),
])
def test_a_surface_identifier_compared_with_a_string_hits(source, want):
    """``params.candidates == "spatial"`` passed under the identifier exemption (round 2)."""
    assert dict(scan_source(source, tier_a=True)) == want


def test_the_engine_hooks_are_exempt_as_identifiers_only():
    from orsim.lifecycle import ORSimAgent

    for name in ENGINE_HOOK_IDENTIFIERS:
        assert callable(getattr(ORSimAgent, name, None)), f"{name}: not an ORSimAgent hook (stale exemption)"
    src = "class A(Base):\n    def entering_market(self, t):\n        super().entering_market(t)\n" \
          "    def exiting_market(self):\n        pass"
    assert dict(scan_source(src, tier_a=True)) == {}
    assert dict(scan_source('hook = "entering_market"', tier_a=True)) == {"sub:market": 1}
    assert dict(scan_source("entering_market_v2 = 1", tier_a=True)) == {"sub:market": 1}


def _fake_repo(tmp_path, files):
    for rel, text in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


def test_only_the_quarantine_prefix_is_exempt(tmp_path, monkeypatch):
    """G1: ``dispatch/compat/``, ``framework/compat/`` are tier A; only ``policies/compat/`` is not."""
    pol = "apps/container_logistics/policies"
    _fake_repo(tmp_path, {f"{pol}/compat/legacy.py": "", f"{pol}/dispatch/compat/branch.py": "",
                          f"{pol}/framework/compat/__init__.py": "", f"{pol}/dispatch/deep/compat/x.py": "",
                          f"{pol}/compatx/y.py": ""})
    monkeypatch.setattr(guard, "REPO_ROOT", tmp_path)
    assert guard.is_compat(tmp_path / pol / "compat" / "legacy.py")
    assert not guard.is_compat(tmp_path / pol / "dispatch" / "compat" / "branch.py")
    assert not guard.is_compat(tmp_path / pol / "compatx" / "y.py")
    listed = {guard.dotted(p) for p in guard._tier_a_root_paths()}
    p = "apps.container_logistics.policies"
    assert {f"{p}.dispatch.compat.branch", f"{p}.framework.compat", f"{p}.dispatch.deep.compat.x"} <= listed
    assert f"{p}.compat.legacy" not in listed


def test_the_tier_a_closure_scans_a_new_sibling(tmp_path, monkeypatch):
    """G2: a module ``app.py`` imports is tier A without anyone listing it; the stops hold."""
    pol, asg = "apps/container_logistics/policies", "apps/container_logistics/assignment"
    _fake_repo(tmp_path, {
        "apps/__init__.py": "", "apps/container_logistics/__init__.py": "",
        f"{pol}/__init__.py": "", f"{pol}/framework/__init__.py": "",
        f"{pol}/compat/__init__.py": "", f"{pol}/compat/legacy.py": "import apps.container_logistics.helper2\n",
        f"{pol}/solver_lib/__init__.py": "", f"{pol}/solver_lib/pair_leaf.py": "",
        f"{asg}/__init__.py": "",
        f"{asg}/app.py": ("from . import publish_helpers\nfrom . import constraints\n"
                          "from apps.container_logistics.policies.compat.legacy import x\n"
                          "from apps.container_logistics.policies.solver_lib import pair_leaf\n"),
        f"{asg}/accounting.py": "",
        f"{asg}/publish_helpers.py": "def f():\n    from .deeper import g\n",
        f"{asg}/deeper.py": "",
        f"{asg}/constraints.py": "import apps.container_logistics.helper3\n",
        "apps/container_logistics/helper2.py": "", "apps/container_logistics/helper3.py": "",
    })
    monkeypatch.setattr(guard, "REPO_ROOT", tmp_path)
    added = dict((m, i) for m, _p, i in guard._tier_a_closure.__wrapped__())
    a = "apps.container_logistics.assignment"
    assert added[f"{a}.publish_helpers"] == f"{a}.app"
    assert added[f"{a}.deeper"] == f"{a}.publish_helpers", "a local import inside a function counts"
    assert f"{a}" in added, "the parent package Python executes on the way"
    for stopped in (f"{a}.constraints", "apps.container_logistics.helper3",          # the boundary
                    "apps.container_logistics.policies.compat.legacy", "apps.container_logistics.helper2",
                    "apps.container_logistics.policies.solver_lib.pair_leaf"):
        assert stopped not in added, stopped


def test_the_real_closure_is_scanned_and_its_boundary_is_not_stale():
    closure = guard.closure_modules()
    a = guard.tier_a_modules()
    assert set(closure) <= set(a)
    # What the closure adds today: the agent shell and manager, the package inits, the shared
    # helpers app.py and the run stats use (each scanned under tier A's rules).
    assert {"apps.container_logistics.assignment", "apps.container_logistics.assignment.agent",
            "apps.container_logistics.assignment.manager", "apps.common.user_registry",
            "apps.container_logistics.rebate"} <= set(closure), sorted(closure)
    assert not set(guard.CLOSURE_BOUNDARY) & set(a)
    importers = set(a)
    reached = set().union(*(guard.static_imports(m, p) for m, p in a.items() if m in importers))
    assert set(guard.CLOSURE_BOUNDARY) <= reached, (
        f"stale boundary entries (nothing in tier A imports them): {sorted(set(guard.CLOSURE_BOUNDARY) - reached)}")
