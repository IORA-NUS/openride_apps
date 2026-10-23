# Writing a planner solver

> **Status: built on branch `feat/policy-slots` (backend) / `feat/policy-slots` (analytics),
> NOT merged into the main tree, not live yet.** This guide describes the generic solver
> contract as of the branch's WS8 commit (base `b70c2ec`, plan `plan_contract_final.md`).
> The main tree still runs the pre-v2 code, and no live run has used this contract.

A **policy** answers one simulation decision; a **slot** is the decision. There is one slot,
`planner`: given every free truck and every unassigned order this tick, which truck serves
which order. Idle trucks, the facility queue, unserved-demand exit and durations are **not
slots**; they are the simulation's own behaviour (the truck state machine and
`idle_trip_manager`, `statemachine/facility_queue_sm.py`, the order lifecycle, OSRM plus
`haul_trip_duration.py`). The slot was called `dispatch` until 2026-10-22, and the old name is
refused, not aliased. Python identifiers keep the old word (`policies/dispatch/`,
`DispatchPolicy`, ...).

The framework calls **one solver per tick** and gets **pairs** back. It does not know which
solver it runs: it never branches on a solver's name, reads no solver concept (cooperation
structure, pool, market, candidate kind), and imports no solver package. Everything a solver
needs from the scenario it reads from the run world; everything the framework needs from a
solver, the solver declares once, at run start.

"Message N" below refers to the numbered refusals (1-22 in round-2 spec §3.7, 23-26 in
`plan_contract_final.md`). Their exact texts are in §6.

## 1. The contract

The minimal author surface. Everything else in this guide is **advanced**.

| You provide | Meaning |
|---|---|
| `@policy(slot="planner", name=..., version=..., description=...)` | the registry entry (§4) |
| a nested `Params` (frozen dataclass of `PolicyParams`, may be empty) | the param schema (§2.2) |
| `solve_tick(self, tick, params) -> Result` | one decision; `params` is the node's validated `Params` |

| You read from `tick` | Meaning |
|---|---|
| `tick.raw_trucks`, `tick.raw_orders` | the documents of this decision (tuples, **read-only**) |
| `tick.trucks`, `tick.orders` | typed views of the same documents (`TruckView` / `OrderView`: `id`, `haulier_id`, `size`, `position` / `pickup` / `dropoff` as `(lon, lat)`, `last_dropoff_position`, `last_dropoff_facility`, facility names, `.raw`) |
| `tick.permitted(truck, order)` | may this pair be committed (ownership-or-declared AND physical feasibility); views or documents |
| `tick.measure.cost(t, o)`, `tick.measure.deadhead_km(t, o)` | the framework's cost (`constraints.assignment_cost` with the run's dials) and haversine deadhead (`inf` without geometry) |
| `tick.rng` | the decision's RNG (§8) |

| You return | Meaning |
|---|---|
| `Result.from_pairs([(truck_doc, order_doc), ...])` or `Result(pairs=((truck_id, order_id), ...))` | committed pairs, in commit order. Leaving an order out defers it (it stays owned and unassigned). |

Plus the battery's determinism declarations when they apply: `order_sensitive=` (output
depends on input order), `module_rng=` (draws the `random` module), and a `perf_budget_ms=`
(default 2000) / `arm_budgets_ms=` that your solver meets on the 1000x500 `scale` fixture
(§9).

The class is constructed as `cls(params=<validated dict>)` (a node with an `inner` ref as
`cls(params=..., inner=<built child>, inner_node=<child node>)`), so it must accept those
keywords. The helper base `DispatchPolicy` (`policies/dispatch/base.py`) provides that
constructor, a default `bind`, the injection API (`set_rng`, `set_tiebreak`,
`set_rebate_book`) and `decision_rng`. It is optional: registration requires only a callable
`solve_tick` (`registry._check_slot_base`). **Not** `solve`: that is the pair API of
`solver_lib.pair_solver.PairSolver` (`solve(trucks, orders, *, pair_allowed, cost)`), which
the two built-in leaves implement and call from their own `solve_tick`.

What the framework then does, identically for every solver (§6): checks the result
(`invariants.check_result`: in-scope ids, no truck or order twice, every pair `permitted`),
discards it on a violation (message 18), commits the pairs, prices the share accounting, folds
the top's run stats, publishes.

## 2. Write a leaf (worked example)

### 2.1 The example

One file in `apps/container_logistics/policies/dispatch/library/` (or
`apps/container_logistics/assignment/solver/`). Those are the only two packages discovery
scans (`registry.BUILTIN_POLICY_PACKAGES`): `discover_builtin_policies` imports every module
there in sorted order, skips names starting with `_`, and lets an import error propagate (a
broken solver must not vanish from the catalog). No framework file is edited to add a solver;
`tests/test_solver_contract_dropin.py` proves it by copying the working tree, writing a leaf
into `library/` and running `python -m openride policy validate` on it in a subprocess.

```python
from __future__ import annotations

import math
from dataclasses import dataclass

from apps.container_logistics.policies.dispatch.base import DispatchPolicy
from apps.container_logistics.policies.dispatch.result import Result
from apps.container_logistics.policies.framework import PolicyParams, param, policy


@policy(slot="planner", name="NearestFirst", version="1",
        description="Orders in id order; each takes its cheapest free permitted truck.",
        badge="central")
class NearestFirst(DispatchPolicy):
    @dataclass(frozen=True)
    class Params(PolicyParams):
        max_km: float | None = param(None, gt=0,
                                     help="Ignore pairs costing more than this (km); unset = no limit.")

    def solve_tick(self, tick, params) -> Result:
        free = list(tick.raw_trucks)
        pairs = []
        for order in sorted(tick.raw_orders, key=lambda o: str(o["_id"])):
            best = None
            for truck in free:
                if not tick.permitted(truck, order):
                    continue
                cost = tick.measure.cost(truck, order)
                if not math.isfinite(cost) or (params.max_km is not None and cost > params.max_km):
                    continue
                key = (cost, str(truck["_id"]))       # ties broken by id: order-independent
                if best is None or key < best[0]:
                    best = (key, truck)
            if best is not None:
                free.remove(best[1])
                pairs.append((best[1], order))
        return Result.from_pairs(pairs)
```

Verified (scratch registration under `framework.registry.isolated_registry`, battery via
`testing.battery.run_battery`, on the WS8 branch): `NearestFirst` **28 PASS, 1 SKIP** (the
SKIP is `contract.scope_audit`: a leaf builds no sub-ticks). With it registered, `Partitioned`
reads **71 PASS, 4 SKIP** and `PooledMarket` **228 PASS, 8 SKIP**, because they battery-check
every registered leaf as an `inner` arm (`[inner=NearestFirst]`); every one of those arm rows
passed except `Partitioned`'s `determinism.order_independent[inner=NearestFirst]`, a SKIP
because `Partitioned` declares `order_sensitive` and that declaration composes (§8).

Notes on the example:

- It reads only the minimal surface: documents, `permitted`, `measure`. It draws no
  randomness, so `determinism.no_module_rng` and `order_independent` pass without
  declarations.
- It is O(trucks x orders): fine for a demonstration, too slow for a real tick. The
  built-in leaves use candidate helpers instead (`solver_lib.candidates`, §3.6).
- It declares no cross-company pairs (the default `bind`). As a TOP solver on a cooperative
  scenario it therefore commits own-company pairs only: `tick.permitted` refuses every
  cross-company pair the top did not declare. As the `inner` of `Partitioned` or
  `PooledMarket` it sees whatever its caller's sub-tick permits. To plan centrally across
  partner companies it must declare them (§3.1), or subclass `solver_lib.pair_leaf.PairLeafMixin`,
  which declares the active arm's pairs the way the built-in leaves do.

The same class is usable three ways, with no code change:

| Selection | Label (stamp) | Badge (stamped provenance of the top) |
|---|---|---|
| `--policy planner=NearestFirst` | `NearestFirst` | `central` (whole tick; a **new arm**) |
| `--solver NearestFirst` (or `--policy-param planner.inner=NearestFirst`) | `Partitioned/NearestFirst` | `per group` |
| `--policy planner=PooledMarket --policy-param planner.inner=NearestFirst` | `PooledMarket/NearestFirst` | `per company` |

A leaf must not assume which of these it is in.

### 2.2 `Params`

- A nested **frozen** dataclass subclassing `PolicyParams`. Every field uses
  `param(default, *, help, ge, gt, le, lt, choices, kind, leaf_only)`; `help` is mandatory.
- Defaults are validated at import. Unknown keys, `bool` for numbers and non-finite values
  are refused everywhere (spec compile, run start, CLI, dashboard).
- Types: `bool`, `int`, `float`, `str`, the nullable numbers `float | None` / `int | None`
  (`None` = unset), and `PolicyRef` (§3.3). `str | None` is not supported.
- Heavy dependencies: import inside `solve_tick` and name them in `requires=` (a missing
  module marks the solver unavailable in the catalog and refuses a run that selects it,
  instead of breaking discovery).

### 2.3 Also required for a new solver (tests, not runtime)

1. A golden entry: `venv/bin/python tests/golden/capture_policy_golden.py --policy <Name>`
   (`tests/test_policy_golden_versions.py` fails without it, by design).
2. Nothing else. The arm list and the exact-message tests read their name lists from the live
   registry (`tests/policy_registry_names.py`). A new arm of a **built-in** must still be
   acknowledged in `_WANT_ARMS`.
3. The name must match `[A-Za-z_][A-Za-z0-9_.-]*` (`registry.POLICY_NAME_PATTERN`): a name
   is a run-name segment and a node of the `A/B` label.

## 3. Advanced

### 3.1 `bind(params, world, *, top) -> Declaration`

Called ONCE per node per process at run start (sim main, the run-start check, the control
plane's dry run and the assignment agent all call `dispatch.bind.bind_profile` /
`bind_tree`), pre-order, with `top=True` for the selection's root. It must be **pure in
`(params, world)`**: no RNG, no mutation, no I/O (the battery's `contract.bind_pure` binds
twice and compares, and checks `random.getstate()` and a deep copy of the profile).
`DispatchPolicy.bind` returns `Declaration()` plus a `candidate_hint` (below).

`Declaration` (`dispatch/bind.py`), every field defaulting to "nothing":

| Field | Who may set it | Meaning |
|---|---|---|
| `cross_company` | TOP only (an inner may declare a subset of the top's) | `frozenset` of `(owner_haulier, carrier_haulier)`: the carrier's trucks may serve the owner's orders. Must lie inside the ceiling (message 23; a malformed entry, e.g. a list, is its malformed form, not a `TypeError`). An inner's non-empty subset narrows its own sub-ticks (`permitted` and `serves`) to own-or-those pairs; an empty one (every built-in inner's) inherits the caller's. |
| `candidate_hint` | any node | read by a CALLER through `tick.callee(ref).declaration.candidate_hint`. `PooledMarket` prebuilds spatial pairs once per round for an inner that hints `"spatial"`. Not validated. On the declaration, not in the registry, because it depends on params. |
| `provenance` | any node | plain JSON, stamped as `run_config.meta.policies.planner.provenance[<node path>]` (paths `planner`, `planner.inner`). The built-ins declare `{badge, sample_scope, candidate_settings}`. |
| `stamp_settings` | TOP only | merged into the run's settings (`orsim_settings` -> `run_config.meta.simulation_settings`), e.g. the built-ins' `COOPERATION`. A key the run already sets (`POLICIES`, `STREAM_GEO`, ...) is refused, exit 2. |
| `run_name_segments` | TOP only | appended to the run name, idempotent by segment. The built-ins append the active arm id. |
| `run_stats` | TOP only | a run-stats aggregator CLASS (§3.5); its `meta_key` must be a plain identifier and not a key the framework writes under `run_config.meta` (`run_stats.RESERVED_META_KEYS`: `policies`, `simulation_settings`, ...), refused at bind |
| `wire_note_keys` | TOP only | the `solver_notes` keys this solver writes into payload fields the framework publishes itself (`PooledMarket`: `pool_id`, `market_round`). Any other note key naming such a field is refused at the tick (message 18), §3.5 |

A `bind` that raises anything but a `PolicyError` is re-raised as `dispatch.bind.BindError` (a
`PolicyError`) naming the node, so the run start refuses it like every other bind refusal. A
`bind` after which the assignment profile differs (fingerprinted before the first bind) is a
`BindError` too; the ceiling is computed before any bind runs. The top's `@policy(badge=...)` is
added to its stamped provenance when its `bind` declared no `badge` (a contract-only solver);
`sample_scope` / `candidate_settings` are never invented for it.

**Ownership is never a solver input.** The haulier is a field of every truck and order
document (`constraints.haulier_of`); the framework reads it off the two documents. A pair is
`permitted` at the top when it is OWN (both hauliers present and equal) or its
`(owner, carrier)` is in the top's `cross_company`, AND it is physically feasible
(`constraints.pair_allowed_ignoring_haulier` with `max_travel_time_pickup`). There is no
company map, haulier list or `owner_of` on the tick, by decision.

**The ceiling** is `constraints.declared_partnerships(profile)`: the union over every
structure the scenario declares (`cooperation.structures[*]`) of
`{(owner, carrier) : owner in share_adjacency(s)[carrier], owner != carrier}`. A solver may
declare less than the active arm (or nothing), never more than the scenario's whole menu.

### 3.2 `RunWorld` and the bind-reads rule

`world` (also `tick.world`) is a read-only, **read-tracking** view of the assignment profile
minus the selection keys (`policies`, `policies_format`, `policy_capture`, and compat's legacy
`strategy`, `solver_params`, `planner`, `use_spatial_matching`). Cooperation reaches a solver
only as `world.get("cooperation")`; the candidate knobs as `spatial_cell_deg`,
`spatial_candidates_per_order`, `spatial_max_rings`, `max_trucks_per_haulier`. Every key read
through `[]`, `get` or `in` is recorded (an absent key too); iterating (`list(world)`,
`len(world)`) reveals the key list and records nothing. Every value is the world's own deep
copy (mutating it changes nothing anyone else sees). Bulk access (`keys()`, `items()`,
`values()`, `dict(world)`, `**world`) raises `TypeError`: read the keys you use one by one. There
is no unrecorded read path on the object (`dispatch.bind.world_snapshot` is framework-only).

**Rule: a `bind` reads every world key its `solve_tick` will read.** The keys a node read at
bind are frozen on its `BoundNode.world_reads`; the tree's union is stamped
(`world_reads`), printed (`Run declaration: ... world_reads=<keys>`), keys the runner cache,
and decides message 25: a per-run override that patches a key no node read (today only
`--structure`, which patches `cooperation`) is refused, because every value of an unread key
runs the same experiment. The battery's `contract.world_reads_at_bind` checks that the keys
read during `solve_tick` are a subset of those read at `bind`, on every fixture.

Consequence: whether the Run step's structure dropdown means anything for a selection is
derived from what the tree's `bind` reads, never from a name list. The four built-ins read
`cooperation`; a solver that does not is refused `--structure` (message 25), and the control
plane's `run-preflight` reports `world_reads` so the dashboard can disable the dropdown.

### 3.3 Nesting: `restrict`, `call`, `callee`, `docs`, `note_incident`

A solver with a `kind="planner"` ref param is a composite:

```python
from apps.container_logistics.policies.framework import PolicyRef, ref

inner: PolicyRef = param(ref("GreedyNearest"), kind="planner", leaf_only=True,
                         help="The per-company solver.")
```

- `kind` is required on, and only allowed on, a `PolicyRef` field: `planner`, or a component
  kind `market.offer` / `market.claim` / `market.arbitration` (§3.7).
- `leaf_only=True` refuses a composite target (message 2). Every built-in `inner` sets it.
- **Single `inner`**: a node delegates through ONE planner ref named `inner`
  (`registry.INNER_PARAM`); `tick.call` resolves only that child
  (`BoundTree.child_for`). A composite with a planner ref not named `inner` is refused at
  resolution. Nested decompositions and per-company inners are a documented limit (§13).
- In JSON a ref value is `{type, params?}` (compiled/stamped refs also carry `version`,
  ignored on read).

The calls, all on `Tick` (`dispatch/tick.py`):

| Call | Meaning |
|---|---|
| `tick.restrict(*, trucks, orders, scope, sample_rng, candidates=None, narrow=None)` | a fresh, unbound sub-tick over a SUBSET of this tick's documents (by identity, else `DispatchContractViolation`). `scope` is free text (your key, e.g. `company:h1`). `sample_rng` is required (`None` = the sub-tick's own `rng`). `candidates` are caller-built pairs (a hint, not validated). `narrow(t, o)` narrows `permitted`; it can never widen it. Draws no RNG. |
| `tick.call(ref, sub)` | solve `sub` with the child `ref` under the same contract: binds the child's params, `rng`, `tiebreak_seed`, `rebates` (its INSTANCE's) onto `sub`, offloads through `sub.execute` when the child's `offload` is `self`, checks the result. Returns the `Result`, or `None` when it was discarded (message 19). A sub-tick serves one call; one not made by this tick's `restrict` is refused. Any non-contract exception propagates. |
| `tick.callee(ref)` | the bound child (`BoundNode`: `name`, `version`, `params`, `declaration`, `world_reads`) |
| `tick.docs(result)` | `result`'s pairs as this tick's documents |
| `tick.note_incident()` | count one swallowed solver error (`stats["crashes"]`); log it yourself |

Worked composite (verified the same way as §2.1: **49 PASS, 3 SKIP**, the skips being
`contract.scope_audit` on two arms and `determinism.order_independent[inner=RandomAssignment]`,
which `RandomAssignment` declares `order_sensitive`):

```python
from __future__ import annotations

from dataclasses import dataclass

from apps.container_logistics.assignment.constraints import haulier_of
from apps.container_logistics.policies.dispatch.composite import CompositePolicy
from apps.container_logistics.policies.dispatch.result import Result
from apps.container_logistics.policies.framework import PolicyParams, PolicyRef, param, policy, ref


@policy(slot="planner", name="PerCompany", version="1",
        description="Each company decides its own orders with its own trucks; no cooperation.",
        badge="per company")
class PerCompany(CompositePolicy):
    @dataclass(frozen=True)
    class Params(PolicyParams):
        inner: PolicyRef = param(ref("GreedyNearest"), kind="planner", leaf_only=True,
                                 help="The per-company solver.")

    def solve_tick(self, tick, params) -> Result:
        self.inner.set_rng(tick.rng)            # the tick's Random, never the random module
        self.inner.set_tiebreak(tick.seed)
        companies = sorted({h for h in map(haulier_of, tick.raw_orders) if h is not None})
        pairs, notes = [], {}
        for hid in companies:
            sub = tick.restrict(
                trucks=[t for t in tick.raw_trucks if haulier_of(t) == hid],
                orders=[o for o in tick.raw_orders if haulier_of(o) == hid],
                scope=f"company:{hid}", sample_rng=tick.rng)
            result = tick.call(params.inner, sub)   # None: that company's result was discarded
            if result is not None:
                pairs.extend(result.pairs)
                notes.update(result.solver_notes)
        return Result(pairs=tuple(pairs), solver_notes=notes)
```

- `CompositePolicy` (`dispatch/composite.py`, optional) is built as
  `cls(params, inner=, inner_node=)`, holds `inner` and forwards each injection to it when
  the inner offers that method. The inner may be any registered class (a contract-only leaf
  without the `DispatchPolicy` base included; `tests/test_contract_only_inner.py`), and the
  framework never `isinstance`-checks either.
- **`PooledMarket` needs the injection API on its inner.** It seeds its inner with
  `set_rng(tick.rng)` / `set_tiebreak(tick.seed)` before any draw, so an inner without
  them is refused at run start (`PooledMarket.__init__`, when the tree is built: `PooledMarket: its inner <Class> has no
  set_rng / set_tiebreak. ...`). Subclass `DispatchPolicy`, or define both methods.
  `Partitioned` does not inject (its groups keep the inner's own RNG), so it takes any leaf.
- **Randomness.** A sub-tick's `rng` is the callee INSTANCE's injected RNG (the `random`
  module by default), so inject the tick's into the inner once per tick (`set_rng(tick.rng)`,
  `set_tiebreak(tick.seed)`) and pass `sample_rng=tick.rng`. Without `set_tiebreak` a
  `GreedyNearest` inner falls back to a shuffle tiebreak and fails
  `determinism.order_independent`. Only `Partitioned` passes the `random` module (pre-v2 byte
  identity, declared `module_rng`).
- `PerCompany` reads no `cooperation`: its bound `world_reads` are the four candidate knobs
  its GreedyNearest inner read, and `--structure` on it is refused with message 25 (verified).
- A decomposition may define `audit_scopes(scope_log, tick) -> list[str]`; the battery's
  `contract.scope_audit` runs it over the sub-ticks it built and SKIPs without one.
  `Partitioned` and `PooledMarket` define one. It certifies the declared shape; it cannot prove
  a third-party solver does not peek (e.g. by keeping a reference to the tick).

### 3.4 `offload`

`@policy(offload="self" | "children")`. Default: `children` for a node with a planner ref,
else `self`. `self`: the framework runs this node's `solve_tick` through `tick.execute` (the
eventlet `tpool` under `solve_in_thread`, off by default; a plain call otherwise).
`children`: the node is called directly and `tick.call` offloads each child call. Both
built-in decompositions are `children`; with the flag off nothing changes.

### 3.5 `solver_notes`, `tick_notes`, `run_stats`

- **`Result.solver_notes`**: `{(truck_id, order_id): {str: JSON}}`, keys a subset of the
  result's pairs (else message 18/19). The framework never interprets them. The wire layer
  (`compat/wire.py`) routes two key NAMES, `pool_id` and `market_round`, to their old
  top-level payload fields, and accepts them only from a top that declared them in
  `Declaration.wire_note_keys` (`PooledMarket` does); a note key naming any field the app
  publishes from its own share tag (`shared`, `owner_haulier_id`, `benefit_km`, ...) that the
  top did not declare discards the tick (message 18), and so does a note that is not plain
  JSON (an object, NaN, infinity). Every other key rides in a `solver_notes` tag key / payload field
  (`AssignedHaulTripPayload.solver_notes`), which is **omitted when empty**, and the truck
  agent copies it into the trip's `collaboration` block. **Limit: notes surface only on SHARED
  (cross-company) pairs**; a note on a self-serve is dropped. A composite must merge its inner
  results' `solver_notes` if they should survive (`Partitioned` does).
- **`Result.tick_notes`**: per-tick JSON, read ONLY by the top's run-stats aggregator.
- **`Declaration.run_stats`** (top only, message 24): a class implementing
  `dispatch.run_stats.RunStatsAggregator` (`meta_key`, `fold(tick_notes, clock) -> forced`,
  `push_due(*, force=False)`, `mark_pushed()`, `snapshot()`, `empty_snapshot()`). The app
  holds one instance per `meta_key`, folds every committed tick (never a discarded one,
  message 18), writes `snapshot()` to `run_config.meta.<meta_key>` when due, and force-flushes
  at agent close. `PooledMarket`'s `_market_stats.RoundStatsAggregator` (`meta_key="market"`)
  is the built-in one. An aggregator that raises (construction, `fold`, a push) is logged at
  ERROR (`RUN STATS DISABLED`), retired for the run, and its `run_config.meta.<meta_key>` is
  overwritten with `{"invalid": true, "error": ...}`; the tick and its pairs are unaffected. `tick_notes` and `run_stats` of a nested node are not consumed: they are
  top-only by design.

### 3.6 Solver helpers (`policies/solver_lib/`)

The framework never imports these (decision (f), `tests/test_framework_imports.py`); solvers
may. `candidates` (`CandidateSettings.from_world`, `spatial_pairs(tick, settings)`,
`tick_sampled_trucks`, `tick_full_pairs`), `objective` (`deadhead_km`, `dual_cycle_cost_fn`,
`is_dual_cycle`), `cooperation` (`resolve_arm`, `validate_arm` = message 14, `directed_pairs`,
`stamp`, `arm_declaration_fields`), `pair_leaf.PairLeafMixin` (the built-in leaves' `bind`),
`pair_solver.PairSolver` (the pair API + a default `solve_tick`), `provenance.node_provenance`.

### 3.7 Components of `PooledMarket`

| Kind | Built-ins | Default | Params |
|---|---|---|---|
| `market.offer` | `OfferAll`, `OfferNone`, `OfferSpare` | `OfferAll` | `OfferSpare`: `keep_below_km: float \| None`, `only_when_short: bool` |
| `market.claim` | `ClaimAllPlanned`, `ClaimNone`, `ClaimIfGainExceeds` | `ClaimAllPlanned` | `ClaimIfGainExceeds`: `min_gain_km: float` |
| `market.arbitration` | `LowestCost`, `OwnerFirst`, `HighestBenefit`, `RandomAward` | `LowestCost` | none |

Components are `PooledMarket` params (not slots), validated against the live registries
through `framework/components.py`; each owner module declares its kind with
`register_component_kind(..., order=N)`. `instantiate_component` is loud on an unknown name
(message 3). `openride policy list market.offer` lists a kind.

### 3.8 Legacy `decide(ctx)` solvers

A class that only defines `decide(ctx) -> Plan` still runs: `DispatchPolicy.solve_tick` wraps
the tick in `compat.legacy_context.LegacyDecideContext` (which serves the leaf-style members:
`raw_trucks`, `allowed`, `accounting_cost`, `candidate_pairs`, `sampled_trucks`, `rng`, ...)
and converts the `Plan` (`compat.legacy_context.Plan`, its only home) to a `Result`; a chain
longer than one is a contract violation. The old composite internals (`ctx.world`, the scope
builders) are gone; write a composite against `tick.restrict` / `tick.call`.

## 4. `@policy` arguments

| Argument | Required | Meaning |
|---|---|---|
| `slot`, `name`, `version`, `description` | yes | Bump `version` when output changes for the same input (§12). |
| `badge`, `badge_tooltip` | no | Display text for the catalog / dashboard option (`"central"`, `"per group"`, `"per company"`); never read at run time. A tooltip needs a badge. What a RUN shows is the top's stamped `provenance.badge`, which the built-ins declare in `bind`. |
| `offload` | no | §3.4 |
| `module_rng` | no | a stated reason the decision draws the `random` module; covers only this node's own draws (and a composite's scopes' `sample_rng` draws); reported SKIP (§8) |
| `order_sensitive` | no | a stated reason output depends on input order; `order_independent` is SKIP |
| `requires` | no | optional imports; missing -> catalog "unavailable", selection refused |
| `perf_budget_ms` | no, default 2000 | battery budget for the default arm (§9) |
| `arm_budgets_ms` | no | budgets for other arms, keyed `"param=value"`; unknown keys are refused at registration (message 17) |

Retired: `information_model` (replaced by `badge` plus stamped provenance) and
`share_tag_format` (the format is compat's name rule, §7). One class backs exactly one name.

## 5. `Tick` reference (advanced members)

Beyond §1: `tick.serves(carrier_id, owner_id)` (own, or a declared pair); `tick.world` (§3.2);
`tick.clock` (`sim_clock`, `time_step`, `horizon_steps`); `tick.seed` (`blake2b(run_id |
time_step)`, inherited by sub-ticks); `tick.tiebreak_seed`; `tick.rebates` (the instance's
injected rebate book, `None` unless `planner.rebate_aware`); `tick.sample_rng`;
`tick.candidates` (caller-supplied pairs or `None`); `tick.scratch` (a per-tick-object dict for
your memos; the framework never reads it); `tick.depth` (0 at top), `tick.scope` (`"tick"` at
top, the caller's key below); `tick.id_index` (`(trucks_by_id, orders_by_id)`, first document
per id); `tick.execute(fn, *args)`; `tick.params` (for the legacy shim).

Deliberately absent: `owner_of` or any company/haulier map, the cooperation
rule/structure/components/pools, candidate kinds, `candidate_pairs`. Not available yet:
`tick.travel`, facility ids, order time windows, held plan tails (the `plan` timing was
removed; only `online` exists).

**The tick and its documents are read-only, and that is CHECKED** (fix loop, review H2).
Every tick attribute is a read-only property (assigning one, or reaching for an instance
`__dict__`, is a `DispatchContractViolation`: message 18 at the top, 19 in a sub-tick); the
framework checks with its own predicate, index and declared pairs, never with anything read
back from the tick. Every document is fingerprinted when the tick is built: a sub-tick result
pairing a document modified since is discarded, and the whole tick is discarded if a paired
document or ANY of its trucks was modified (the share accounting prices the owner reserve over
every truck after the commit), so the top
check's trust in a sub-tick's physical verdict (`tick.call` records the pairs by document
identity) is sound. Build copies and map their ids back. `tick.scratch` is the one mutable
member. `tick.serves(carrier, owner)` is `False` when either id is missing.

## 6. What the framework owns, and its refusals

Ownership (an order's haulier never changes), feasibility (`permitted` re-checked on every
committed pair, whatever produced it), invariants (`check_result`), commit, accounting (§7),
run stats driving, the stamp. Run-start refusals exit 2 before any agent spawns; per-tick
discards log ERROR agent-side: `journalctl --user -u openride-celery`, **not** `sim.log`.

- **18** (whole tick discarded): `PLANNER POLICY CONTRACT VIOLATION: <label> v<version> at step <n> returned an invalid tick result: <problems>. The result was DISCARDED (nothing committed this tick). This is a policy bug: run `openride policy validate planner <name>`.`
- **19** (one sub-tick discarded; inside `PooledMarket` one company for one round): `PLANNER POLICY CONTRACT VIOLATION: inner <name> v<version> in scope <scope> at step <n> returned an invalid result: <problems>. The scope's result was DISCARDED (nothing committed for this scope this tick). This is a policy bug: run `openride policy validate planner <name>`.`
- **20** (`PooledMarket` inner crash, owned by that solver): `PLANNER POLICY CRASHED: inner <name> raised for company '<hid>' in round <r> at step <n>; that company commits nothing this round (pooled-market behaviour, unchanged). Traceback follows. (celery journal, not sim.log)`
- **23** (run start; `dispatch/bind.py::_check_top`), two forms:
  - `planner policy <Name> at planner: bind() declared malformed cross-company pair(s) <pairs>; each must be an (owner_haulier, carrier_haulier) pair of two different non-empty strings.`
  - `planner policy <Name> at planner: bind() declared cross-company pair(s) ('<owner>', '<carrier>'), ... (owner, carrier) that no structure of the scenario declares. A solver may let a carrier serve another company's orders only where the scenario's structure menu pairs them (the union of every structure's adjacency); fix the solver's bind() or the scenario's structures.`
- **24** (run start; `_check_inner`): `planner policy <Name> at planner.inner: a nested node's bind() declared <cross-company pair(s) ... the top did not declare | the top-only field(s) stamp_settings, run_name_segments, run_stats>. Only the top node of the selection declares cross-company pairs (an inner may declare a subset), stamp settings, run-name segments and run stats.`
- **25** (run start; `refuse_unread_patches`): `a per-run override patched world key(s) 'cooperation', but the planner selection <Name> (bound at planner) reads no such key (it reads: <keys>). Every value of an unread key runs the same experiment, so the run is refused: drop the override, or select a planner whose bind() reads that key.`
- **26** (run start and dashboard dry run; `scenario_overrides.apply_cooperation_override`), two forms:
  - `cooperation structure override '<X>' is not declared by assignment agent '<id>' (declared structures: <ids>). Pick a declared id or fix the typo (it is never replaced by the scenario's own arm).`
  - `cooperation structure override '<X>': assignment agent '<id>' has no cooperation block (declared structures: none), so there is no arm to select. Drop the override, or declare structures in the scenario's spec.json cooperation block and recompile.`

Messages raised inside the run-start check (`profile.assert_assignment_profiles_resolvable`:
23, 24, 25) are prefixed `assignment agent '<id>': `. Also refused at run start: a stamp
setting colliding with a run setting (`planner policy <Name> declared run setting(s) <keys>
that the run already sets; ...`), and a `PooledMarket` whose inner lacks `set_rng` /
`set_tiebreak` (§3.3; unnumbered, owned by that solver).

**Crashes.** A top solver's, `Partitioned`'s inner's or your own composite's non-contract
exception propagates out of the tick; ORSim's step handler catches it (`Exception in step for
Agent ...`), that tick commits nothing and the agent lives on. A `PooledMarket` inner crash is
message 20; an offer/claim crash is swallowed (`Offer/Claim policy failed for haulier ...`).
Violations and crashes are **not stamped into `run_config`**: a run whose solver never commits
anything can still finish `ok: true`.

## 7. Accounting is not your objective

Computed in `assignment/accounting.py` after the commit, from the committed pairs, so the same
pairs get the same tags whichever solver produced them: `account_shares(pairs, *, candidates,
cost=tick.measure.cost, own_allowed=<strict own-haulier pair_allowed>)`, then
`render_share_tags(accounts, *, fmt, solver_notes)`.

- Cross-company pairs only (both hauliers known and different).
- `owner_reserve_km`: the owner's cheapest free, own-feasible truck after the commit (`None`
  when none); `awarded_cost_km = float(cost(t, o))` (exception -> `inf`); `benefit_km =
  reserve - awarded`, signed, `None` without a reserve, rounded to 0.001 km.
- The cost is `constraints.assignment_cost` with the live dials `AssignmentApp._solver_params`
  (= `profile.solver_params`, the one-dial mirror of the first node declaring the dual-cycle
  params, normally the leaf); offline (battery, bench, capture) the same compat rule
  (`compat/measurement.py`). Stamped as `accounting_params`.
- Two wire formats (`compat/wire.py`), the same values: **`v1`** (4 keys, a non-finite benefit
  stays `-inf`) for exactly a `Partitioned` top, an explicit NAME-keyed compat rule
  (`V1_SHARE_TAG_BUILTINS`; every pre-v2 partitioned run carries these bytes, and no
  registration can opt in); **`audited`** (8 keys, non-finite -> `None`) for every other top.
  Both append a `solver_notes` key only when non-legacy notes exist. Stamped as
  `share_tag_format`.

## 8. Determinism

Draw randomness **only from `tick.rng`** (or `self.rng` inside `with self.decision_rng(tick):`).

- Top: `tick.rng` is `random.Random(tick.seed)`, built on first read (the partitioned
  default never builds it). Sub-tick: the callee instance's injected RNG (§3.3).
- Break ties with `tick.tiebreak_seed` (`assignment/tiebreak.pair_tiebreak`) or a stable key
  (the example uses ids), or declare `order_sensitive="why"`.
- **Module RNG only where declared.** One extra module draw shifts trucks'
  `random() <= response_rate` rolls for the rest of the worker's life. `module_rng` is scoped
  to the node that declares it: the battery attributes each module draw to the nearest
  selection node on the stack; an uncovered draw FAILS naming the node, a covered one is SKIP.
  Limits: a function bound at import (`from random import random`), `random._inst` or a thread
  that existed before the decision cannot be separated (SKIP, never PASS).
- `order_sensitive` composes: any node of the selection declaring it makes
  `order_independent` a SKIP for that arm.
- `determinism.order_independent` holds only while `spatial_candidates_per_order` (default 40)
  does not bind.

## 9. Validate, bench, capture

```
python -m openride policy validate planner NearestFirst   # PASS/FAIL/SKIP table, exit 1 on any FAIL
python -m openride policy bench planner --policies NearestFirst,part=Partitioned \
    --policy-param part.inner=NearestFirst --synthetic three_company_chain,scale [--json]
openride run <slug> --capture-dispatch every=10,max=40    # <datahub>/fixtures/dispatch/<run_id>/step_<n>.json.gz
python -m openride policy bench planner --policies Partitioned,NearestFirst --fixtures <dir>
```

Control-plane equivalents: `python -m openride_control.command policy-validate --slot planner
--name X`, `policy-bench ...`, `validate-run-policies --scenario S [--cooperation-structure X]
...` and `run-preflight --scenario S [--cooperation-structure X] ...` (`{ok, action:
"run_preflight", world_reads, structures: {active, declared}}`).

**Battery ids:** `structure.*` (returns_plan, no_truck_twice, no_order_twice, only_in_scope,
only_allowed_pairs), `robustness.<fixture>` (empty_trucks, empty_orders, single_truck,
no_geometry, one_haulier, all_infeasible, tied_costs, three_company_chain, missing_haulier,
scale), `determinism.{reproducible,order_independent,no_module_rng}`,
`contract.{scope_audit,bind_pure,world_reads_at_bind,declaration_within_ceiling}`,
`performance.scale`, `params.*`. Each row is PASS, FAIL or **SKIP** (a declared exemption: `ok`
stays true, exit 0, "N passed, k skipped"). Read the skip count, not only the verdict. Built-in
counts on the branch: GreedyNearest 50 + 2 SKIP, RandomAssignment 48 + 4, Partitioned 49 + 3,
PooledMarket 205 + 8 (the leaves' SKIPs include `contract.scope_audit`).

**Arms.** Each non-default `choices` value of every `str` param, and for each ref param every
other registered target of its kind satisfying `leaf_only`, at its defaults (from
`battery.policy_arms`). Not crossed, not recursive.

**Budgets.** `performance.scale` runs the 1000x500 fixture, best of 3. Rule:
`max(3 x worst of 2x5 runs, 500)` ms, measurements written beside the decorator (this box
varies 1.3-2x on its own). An unmeasured composite `inner=X` arm gets X's `perf_budget_ms` plus
the composite's. A budget above 2000 ms (`STEP_SETTLE_SECONDS`) makes the assignment agent a
per-step straggler at 1000x500 (`GreedyNearest candidates=sampled`, 5790 ms, is one).

**Bench columns:** `policy, label, badge, fixtures, assigned, orders, assign_rate,
deadhead_km_total, deadhead_km_mean, dual_cycle, cross_company, benefit_km_total, solve_ms_p50,
solve_ms_max, violations`. Deadhead is haversine at decision time, not the road KPI.

**Capture** format `openride.dispatch_context/3`: `run_id`, `time_step`, `sim_clock`,
`horizon_steps`, `tick_seed`, `module_rng_state`, `selection`, `world` (the raw run world: the
whole cooperation menu and active arm, the knobs, `max_travel_time_pickup`),
`accounting_params`, `declaration` (`cross_company` + each path's `candidate_hint`),
`trucks`, `orders`, `live_pairs`, `rebate_aware`. `/2` is read through `compat/capture_v2.py`;
`/1` is refused (message 22). `every=N` counts dispatch ticks, not sim steps.

## 10. Select it

**Scenario (`spec.json`)**: `policies.planner` is the only dial, e.g.
`{"type": "PooledMarket", "params": {"max_rounds": 20, "inner": {"type": "NearestFirst"}}}`.
Slot default: `Partitioned{inner: RandomAssignment}` (`compat/defaults.py`). Compile writes
`policies` plus `policies_format: 2` and the legacy mirrors. A profile with `policies` but
without the marker is refused at run start (recompile, never hand-patch).

**One run** (`openride run`, `run-simulation`, `validate-run-policies`): `--policy
planner=X` sets the top (a **central** planner when X is a leaf: a new arm); `--solver X` /
`ORSIM_SOLVER` and `--policy-param planner.inner=X` set the innermost leaf;
`--policy-param planner.inner.k=v` a nested param. `--structure X` / `ORSIM_COOP_STRUCTURE`
patches `cooperation.active` in memory (refused on an unknown id or a scenario without a
cooperation block, message 26, or a selection that reads no `cooperation`, message 25).
`--sharing-policy` and `ORSIM_SHARING_POLICY` are refused (they were inert at run time:
`planner.sharing` is read only at compile; set it in `spec.json` and recompile).

**Run name.** Legacy segments are byte-identical (`--solver GreedyNearest` -> ` · GreedyNearest`;
`ORSIM_PLANNER_TOPOLOGY` -> ` · pooled`). The top's `run_name_segments` follow, idempotent by
segment: every run whose top solver reads `cooperation` and runs an arm with an id names that
arm (all four built-ins do). **Known limit:** central `--policy planner=GreedyNearest` and
`--solver GreedyNearest` both read ` · GreedyNearest`; tell them apart by the stamp.

**The stamp** `run_config.meta.policies.planner`: `name`, `version`, `kind`, `timing`,
`source` (`bundle` / `legacy-profile` / `override`), `label`, `leaf`, `params` (nested),
`share_tag_format`, `accounting_params`, `provenance` (`{<node path>: <Declaration.provenance>}`;
the built-ins' top carries `badge`, `sample_scope` and `candidate_settings`), `declaration`
(`{cross_company: [[owner, carrier], ...]}`) and `world_reads`. Runs stamped before this
contract carry `information_model` and a top-level `candidate_settings` instead of
`provenance`. Console: `Policies effective: planner=<label> v<version> timing=<t>
params=<json> accounting_params=<json>` and `Run declaration: cross_company=<n> pairs,
world_reads=<keys>`.

## 11. Legacy spellings (compat, translated deterministically)

The pre-v2 keys (`solver`/`strategy`, `solverParams`/`solver_params`, `planner.deployment`,
`planner.topology`, `planner.market`, `use_spatial_matching`) and the legacy env vars
(`ORSIM_PLANNER_TOPOLOGY`, `ORSIM_OFFER_POLICY`, `ORSIM_CLAIM_POLICY`,
`ORSIM_ARBITRATION_RULE`, `ORSIM_MARKET_MAX_ROUNDS`) are translated in `policies/compat/`
(`legacy.py`, `legacy_profile.py`, `legacy_env.py`, `mirrors.py`), the only place framework-side
code may name a solver. Topology absent / `partitioned` -> `Partitioned{inner: S}`; `pooled` /
`two-stage` -> `PooledMarket{...planner.market..., inner: S}`; no `strategy` ->
`RandomAssignment`; `use_spatial_matching: false` -> `inner.candidates = "sampled"`; undeclared
params dropped at WARNING; unknown topology / component / `max_rounds` outside 1..100 are loud.
At run start a compiled `policies` block is authoritative and the mirrors are checked, never
read (message 13).

## 12. Versioning, comparability, restarts

- **Bump `version=`** when output changes for the same input, then re-capture that solver's
  golden entry. A different `inner` is a selection, not a version.
- Every existing scenario at its defaults is byte-identical to pre-v2 (doc 26 §5). A central
  top-level leaf, a `PooledMarket` with another `inner`, or any param change is a **new arm**;
  read the arm from `label` and `provenance` (the top's `badge`), not from the run name.
  Noise is ~4-5% on `empty_ratio`: n>=3.
- **Adding or editing a solver file** needs `systemctl --user restart openride-celery` (agents
  run in the long-lived workers). Selecting a solver or its params never does.

## 13. Known limits

- Single `inner` per node; no nested decompositions; no per-company inner.
- Only the `online` timing exists; held tails / day-ahead planning are not a framework concept
  (a tour solver's wrapper turns tours into pairs).
- `solver_notes` surface only on shared pairs; `tick_notes` / `run_stats` only from the top.
- The scope audit is a tripwire, not a proof; the module-RNG attribution cannot separate a draw
  through a function bound at import.
- Central `--policy planner=X` and `--solver X` give the same run-name segment.
- Violations and crashes are not in `run_config`. Capture refuses rebate-aware ticks.
