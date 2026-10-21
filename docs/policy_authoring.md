# Writing a policy

A **policy** answers one simulation decision; a **slot** is the decision. Only the
`dispatch` slot is implemented today (which free truck serves which unassigned order,
per scope per tick). `market.offer`/`market.claim` are legacy (configured under
`planner.market.*`); `idle`, `queue`, `world.shedding`, `world.duration` are planned
(P5). `openride policy list` shows the live catalog. Plan:
`docs/plans/policy_slots_p0_p3.md`.

## 1. Write it

One file in `apps/container_logistics/policies/dispatch/library/`. Discovery imports
every module there (names starting with `_` are skipped); an import error fails loudly
rather than hiding the policy.

```python
from __future__ import annotations
from dataclasses import dataclass

from apps.container_logistics.policies.dispatch.base import DispatchPolicy
from apps.container_logistics.policies.dispatch.plan import Plan
from apps.container_logistics.policies.framework import PolicyParams, param, policy


@policy(slot="dispatch", name="NearestFirst", version="1",
        description="Each order, in input order, takes its nearest allowed truck.",
        requires=(),             # e.g. ("ortools",): listed as "missing" when not importable
        order_sensitive="serves orders in input order",   # omit if order-independent
        perf_budget_ms=5000)     # battery budget (ms) on the 1000x500 scale fixture: this
                                 # O(trucks x orders) loop takes ~1.4 s there; set
                                 # max(3 x worst measured, 500) and note the numbers
class NearestFirst(DispatchPolicy):
    @dataclass(frozen=True)
    class Params(PolicyParams):
        max_km: float = param(50.0, gt=0, help="Ignore pairs farther than this (km).")

    def decide(self, ctx) -> Plan:
        used, chains = set(), {}
        for order in ctx.orders:
            best = min(((ctx.deadhead_km(t, order), t.id) for t in ctx.trucks
                        if t.id not in used and ctx.allowed(t, order)), default=None)
            if best and best[0] <= ctx.params.max_km:
                used.add(best[1])
                chains[best[1]] = (order.id,)
        return Plan(chains=chains)
```

- `Params` is a nested **frozen** dataclass subclassing `PolicyParams`; every field
  uses `param(default, help=..., ge/gt/le/lt=..., choices=(...))`. Defaults are
  validated at import; unknown keys, `bool` for numbers and non-finite values are
  refused everywhere (spec compile, run start, CLI, dashboard).
- **Heavy dependencies are imported inside `decide`**, never at module top, and named
  in `requires=` so the catalog can mark the policy unavailable instead of breaking
  discovery for every other policy.
- Subclassing `PairSolver` (`policies/dispatch/pair_solver.py`) gives the legacy
  `solve(trucks, orders, pair_allowed=, cost=)` contract and a default `decide`; the
  two built-ins live in `apps/container_logistics/assignment/solver/`.

## 2. What the context gives you (`DispatchContext`)

`ctx.trucks` / `ctx.orders` (read-only `TruckView`/`OrderView`: `id`, `haulier_id`,
`size`, `position`/`pickup`/`dropoff` as `(lon, lat)`, last drop-off, facility names,
`.raw`); `ctx.raw_trucks` / `ctx.raw_orders` (the documents; never mutate them);
`ctx.allowed(t, o)` (framework feasibility: haulier/partnership, size,
`max_travel_time_pickup`); `ctx.candidate_pairs("spatial" | "sampled" | "full")`
(cached per kind; see below); `ctx.sampled_trucks()`; `ctx.deadhead_km(t, o)` (haversine, `inf`
without geometry); `ctx.dual_cycle_cost(t, o, bonus_km=, radius_km=)`; `ctx.params`;
`ctx.rng`; `ctx.tiebreak_seed`; `ctx.scope` (`kind` partition|company, `key`,
`round`); `ctx.clock` (`sim_clock`, `time_step`, `horizon_steps`); `ctx.rebates`.

**Spatial pairs on the pooled topology.** A company scope (`ctx.scope.kind ==
"company"`) has spatial pairs only when the resolved candidate kind is `spatial`: they
are prebuilt once per bidding round, and only then. `ctx.candidate_pairs("spatial")` on
a company scope of any other kind raises `DispatchContractViolation` (logged as a
CONTRACT VIOLATION, plan discarded) instead of quietly returning `[]`. The fix is to
declare a `candidates` param with `"spatial"` among its choices and select it, or ask
for `ctx.candidate_pairs()` (the scope's own kind). A spatial-kind company scope with
no nearby pairs still returns `[]`. Partition scopes build spatial pairs on demand,
whatever the kind.

**P1 limits:** there is no `ctx.travel` (road travel times), no `ctx.facilities`, no
`ctx.state` and no `held` plan tails until P4. The scope (one call per partition or per
company) is a framework decision, not the policy's.

## 3. Plan semantics and invariants

`Plan(chains={truck_id: (order_id, ...)})`; `Plan.from_pairs([(truck_doc, order_doc), ...])`
builds a heads-only plan. Insertion order is commit order. Under `online` timing (the
only executable timing today) **only each chain's head is committed**; tails are for
`plan` timing (P4). **Deferring an order = leaving it out.** `notes` are logged at DEBUG.

The runner checks every plan (`policies/dispatch/invariants.py`): no truck or order
twice, only in-scope ids, non-empty chains, every head `ctx.allowed`. A violating plan
is **discarded for that scope and tick** (nothing committed) and logged as
`DISPATCH POLICY CONTRACT VIOLATION: ...` at ERROR. That line is agent-side: it is in
`journalctl --user -u openride-celery`, **not** `sim.log`. Violations are not yet
stamped into the run's `run_config`.

An exception other than a contract violation behaves differently per topology, and
neither is a violation:

- **partitioned**: it propagates out of `AssignmentApp.assign()`, i.e. out of the tick.
- **pooled**: `pooled_planner` logs it (`Deployment solve failed for haulier '<id>' —
  skipping.`, with the traceback, agent-side) and skips that company for that bidding
  round; the tick and the other companies continue. A policy that crashes on every
  call therefore silently assigns nothing for that company while the run still
  finishes `ok: true`, so grep the celery journal for that line.

Handle empty inputs and missing geometry yourself.

## 4. Determinism

Draw randomness only from `ctx.rng` (or `self.rng`). Never call the `random` module
directly: on the partitioned path the injected RNG *is* the module, and one extra or
missing draw shifts every later draw in the worker, including trucks' response rolls.
On the pooled path `ctx.tiebreak_seed` is set; break ties with it
(`assignment/tiebreak.pair_tiebreak`) so the plan does not depend on input order, or
declare `order_sensitive="why"`.

What the battery's determinism checks do and do not promise:

- `determinism.no_module_rng` injects a private `random.Random` (`set_rng`) and fails if
  `random.getstate()` changed during the decision. It is checked only on fixtures whose
  trucks fit `max_trucks_per_scope` (500): when that cap binds, the partition scope's
  `ctx.sampled_trucks()` draws from the random MODULE **by framework design** (byte
  identity of the partitioned path), so a module-state change there is not yours. On
  the pooled path the sample draws from the tick `Random` instead.
- `determinism.order_independent` holds **only while the spatial per-order candidate
  cap (`spatial_candidates_per_order`, default 40) does not bind**.
  `spatial.iter_candidate_pairs` truncates each order's candidates in input order, so
  on dense real ticks the framework's candidate set itself depends on input order, and
  no policy is order-independent there. The battery's fixtures keep fewer trucks than
  the cap on purpose; a PASS certifies the policy, not the candidate generation.

## 5. Validate, bench, capture

```
openride policy validate dispatch NearestFirst          # PASS/FAIL table, exit 1 on any FAIL
openride policy bench dispatch --policies GreedyNearest,NearestFirst --synthetic scale
openride policy bench dispatch --policies GreedyNearest,NearestFirst --synthetic one_haulier,tied_costs \
    --policy-param NearestFirst.max_km=20 [--json]
openride run <slug> --capture-dispatch every=10,max=40  # writes <datahub>/fixtures/dispatch/<run_id>/step_*.json.gz
openride policy bench dispatch --policies GreedyNearest,NearestFirst --fixtures datahub/fixtures/dispatch/<run_id>
```

The battery (structure, robustness per fixture, determinism, performance, params) is
also `tests/test_policy_contract_battery.py`, parametrized over every registered policy.
Every decision goes through `DispatchRunner.run`, the path the sim executes, and the
structure verdicts are `invariants.check_plan`'s own messages. Besides the defaults,
**every non-default `choices` value of every `str` param is an arm**: the structure,
robustness and determinism checks run again per arm, with the arm in the id
(`structure.no_order_twice[candidates=sampled]`). `performance.scale` gates the default
arm only, because `perf_budget_ms` is measured there; each arm's `robustness.scale` line
reports its decide time.
The bench reports **haversine deadhead, a decision-time proxy, NOT the road-distance
KPI**; single-tick scores screen policies, only runs certify them (n>=3, interleaved).
`--synthetic` takes `scale`, any battery fixture name, a comma list, or `all`. Capture is
also `run-simulation --capture-dispatch` on the control plane and
`ORSIM_CAPTURE_DISPATCH=every=N,max=M` on the sim; a malformed value refuses the run
(exit 2). Capturing adds the hook only when set.

## 6. Select it

- Scenario: `spec.json` `"policies": {"dispatch": {"type": "NearestFirst", "params": {"max_km": 20}}}`
  (or `openride scenario edit <slug> --policy dispatch=NearestFirst --policy-param dispatch.max_km=20`,
  or the dashboard Scenario tab's **Operator policies** card).
- One run: `openride run <slug> --policy dispatch=NearestFirst --policy-param dispatch.max_km=20`
  (`--solver X` = `--policy dispatch=X`); the same flags on `run-simulation`; the sim reads
  `ORSIM_POLICY_OVERRIDES` (JSON `{slot: {type?, timing?, params?}}`). Overrides are
  validated before spawning. A changed `type` starts from the new policy's defaults.
- The effective choice is stamped in `run_config.meta.policies`.

## 7. Versioning, accounting, comparability, restarts

- **Bump `version=`** whenever the policy's output changes for the same input, then
  regenerate its golden entry: `venv/bin/python tests/golden/capture_policy_golden.py --policy NearestFirst`.
  A new policy needs an entry too (`tests/test_policy_golden_versions.py`).
- **Accounting is not your objective.** `benefit_km` and pooled-market arbitration use
  the framework's `constraints.assignment_cost` with `solver_params`, which mirrors only
  the dual-cycle dials. A policy optimising something else still gets scored by that
  (plan §7 risk 8).
- Every slot at its default is byte-identical to before policy slots. A different
  policy, or `RandomAssignment` with `candidates=spatial`, is a **new arm**, not
  comparable with the default; read the arm from `run_config.meta.policies`.
- **Adding or editing a policy file needs `systemctl --user restart openride-celery`**
  (agents import it). Choosing a policy or changing its params never does: it ships as
  data at spawn.
