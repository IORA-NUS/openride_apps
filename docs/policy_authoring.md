# Writing a policy

> **Status: built on branch `feat/policy-slots` (backend) / `feat/policy-slots` (analytics),
> NOT merged into the main tree, not live yet.** Backend HEAD `f8b91b9`. Everything below
> describes the branch; the main tree still runs the pre-v2 code.

A **policy** answers one simulation decision; a **slot** is the decision. There are five
slots: `dispatch` (implemented: given every free truck and every unassigned order this tick,
which truck serves which order) and `idle`, `queue`, `world.shedding`, `world.duration`
(planned, P5). `market.offer`, `market.claim` and `market.arbitration` are **not slots**:
they are *components* of the `PooledMarket` dispatch policy (§3.3).
`openride policy list` shows the live catalog, including components and each dispatch
policy's information model. Plans: `docs/plans/policy_slots_p0_p3.md` (round 1) and
`docs/plans/policy_slots_round2_whole_tick.md` (round 2, this model). "Message N" below
refers to the numbered messages in round-2 spec §3.7.

## 1. The model in one paragraph

Every tick the framework builds **one** whole-tick `DispatchContext`, hands it to **one**
dispatch policy, and gets **one** `Plan` back. The framework then checks the plan against the
world rule, commits the chain heads and computes the share accounting. How the tick is split
up (per cooperation group, per company in a market, or not at all) is **the policy's
choice**. So there are two kinds of dispatch policy:

- A **leaf** decides whatever context it is given. Selected at top level it sees the whole
  tick and is a *central planner*. As the `inner` of a decomposition it decides one scope
  (one cooperation group, or one company in one market round). `GreedyNearest` and
  `RandomAssignment` are leaves.
- A **decomposition** (a *composite*) splits the tick into scopes and calls its `inner` leaf
  on each. `Partitioned` (one scope per cooperation group; **the default**) and
  `PooledMarket` (one scope per company per bidding round, with offers, claims and
  arbitration) are the built-ins.

Why: round 1 let the framework pick the scope (one call per partition or per company), which
hard-wired two topologies into `AssignmentApp` and made a central planner such as NetworkFlow
(P4) impossible to express. Now "Partitioned vs PooledMarket vs central" is an ordinary
policy choice, and the framework is only the part that must not differ between arms:
ownership, feasibility, commit, accounting.

An `inner` must be a leaf (`leaf_only`); decompositions do not nest. That is what makes "the
effective information model is the top-level policy's model" sound (§4).

## 2. Write a leaf (worked example)

One file in `apps/container_logistics/policies/dispatch/library/`. Discovery
(`framework/registry.py::discover_builtin_policies`) imports every module there and in
`assignment/solver/`; names starting with `_` are skipped, and an import error fails loudly.

The example below was dropped into a scratch copy of the branch at `f8b91b9` as
`library/regret_match.py` and passed `openride policy validate dispatch RegretMatch`
(`PASS all 47 checks passed`). With it and the composite of §3.4 registered,
`policy validate dispatch Partitioned` (`67 checks passed (4 skipped)`) and `PooledMarket`
(`207 checks passed (8 skipped)`) also passed, because they battery-check every leaf as an
inner (§9). A skip is reported as `SKIP`, not `PASS` (§9).

```python
from __future__ import annotations

import math
from dataclasses import dataclass

from apps.container_logistics.policies.dispatch.base import DispatchPolicy
from apps.container_logistics.policies.dispatch.plan import Plan
from apps.container_logistics.policies.framework import PolicyParams, param, policy


@policy(slot="dispatch", name="RegretMatch", version="1",
        description="Orders with the most to lose go first: largest gap between their best "
                    "and second-best truck.",
        information_model="central",   # a leaf: correct at top level AND as an inner
        # max(3 x worst measured, 500) on the 1000x500 `scale` fixture, per arm:
        perf_budget_ms=500,                           # spatial: ~95-110 ms
        arm_budgets_ms={"candidates=sampled": 4000})  # sampled: ~1.3 s (250k pairs)
class RegretMatch(DispatchPolicy):
    @dataclass(frozen=True)
    class Params(PolicyParams):
        candidates: str = param("spatial", choices=("spatial", "sampled"),
                                help="Candidate pairs: the spatial index, or a capped truck sample.")
        max_km: float | None = param(None, gt=0,
                                     help="Ignore pairs costing more than this (km); unset = no limit.")

    def decide(self, ctx) -> Plan:
        p = ctx.params
        options: dict[str, list[tuple[float, str]]] = {}
        for truck, order in ctx.candidate_pairs():      # already feasible in THIS scope
            cost = ctx.accounting_cost(truck, order)
            if math.isfinite(cost) and (p.max_km is None or cost <= p.max_km):
                options.setdefault(str(order["_id"]), []).append((cost, str(truck["_id"])))
        for opts in options.values():
            opts.sort()                                  # ties broken by truck id: order-independent

        def urgency(oid: str) -> tuple[float, float, str]:
            opts = options[oid]
            regret = opts[1][0] - opts[0][0] if len(opts) > 1 else math.inf
            return (-regret, opts[0][0], oid)

        used: set[str] = set()
        chains: dict[str, tuple[str, ...]] = {}
        for oid in sorted(options, key=urgency):
            for _cost, tid in options[oid]:
                if tid not in used:
                    used.add(tid)
                    chains[tid] = (oid,)
                    break
        return Plan(chains=chains)
```

Notes on the example:
- `ctx.candidate_pairs()` with no argument uses the scope's resolved kind, which is the
  `candidates` param when declared (else `sampled`). Asking for `"spatial"` explicitly on a
  company scope of another kind raises (§5).
- It draws no randomness, so `determinism.no_module_rng` and `order_independent` pass
  without declarations. A leaf that needs randomness draws from `ctx.rng` (§8).
- The `sampled` arm costs ~1.3 s at 1000×500, above `STEP_SETTLE_SECONDS` (2 s is the
  settle window; anything near it makes the assignment agent a per-step straggler).

The same class is usable three ways, with no code change (all three were dry-run through
`validate-run-policies` and benched):

| Selection | Label (stamp) | Information model |
|---|---|---|
| `--policy dispatch=RegretMatch` | `RegretMatch` | `central` (whole tick, a **new arm**) |
| `--solver RegretMatch` (or `--policy-param dispatch.inner=RegretMatch`) | `Partitioned/RegretMatch` | `per_group` |
| `--policy dispatch=PooledMarket --policy-param dispatch.inner=RegretMatch` | `PooledMarket/RegretMatch` | `per_company` |

A leaf must not assume which of these it is in: read `ctx.trucks`/`ctx.raw_trucks`,
`ctx.orders`, `ctx.allowed`, `ctx.candidate_pairs()`, `ctx.accounting_cost` and `ctx.rng`,
and nothing that exists on only one scope kind. `ctx.scope.kind` is for logging.

**A NetworkFlow (P4) leaf has exactly this shape.** Differences: `requires=("ortools",)`
and `import ortools` *inside* `decide` (a missing module marks the policy unavailable in the
catalog and refuses a run that selects it, `PolicyDependencyError`, instead of breaking
discovery); arcs from `ctx.candidate_pairs()`, costs from `ctx.accounting_cost` (or its own
objective), and `ctx.world.cooperation_pairs(hauliers)` when it builds a company-level MIP.
`ortools` is not installed in this venv, so that variant is untested. P4 also needs context
accessors that do not exist yet (§5).

**Also required for a new policy (tests, not runtime):**
1. A golden entry: `venv/bin/python tests/golden/capture_policy_golden.py --policy RegretMatch`
   (`tests/test_policy_golden_versions.py` fails without it).
2. Nothing else. A new **leaf** adds an `inner=<Leaf>` arm to `Partitioned` and
   `PooledMarket`; the arm list (`expected_arms` in `tests/test_policy_contract_battery.py`)
   and the exact-message tests read their name lists from the live registry
   (`tests/policy_registry_names.py`), so they pick the leaf up without an edit. `_WANT_ARMS`
   still pins the built-ins' own str-choice arms and ref params, so a new arm of a
   **built-in** must be acknowledged there.
3. The name must match `[A-Za-z_][A-Za-z0-9_.-]*` (`registry.POLICY_NAME_PATTERN`); any
   other name raises `PolicyRegistrationError` at registration. The same rule applies to
   component names (`market.offer` etc.). It keeps run-name separators (` · `, `/`, `(`,
   `)`), `=`, `,`, `:`, whitespace and non-ASCII out of labels. Param VALUES in a label are
   JSON with `ensure_ascii=True`, so they are escaped rather than refused.

Rules for `Params`:
- A nested **frozen** dataclass subclassing `PolicyParams`. Every field uses
  `param(default, *, help, ge, gt, le, lt, choices, kind, leaf_only)`; `help` is mandatory.
- Defaults are validated at import. Unknown keys, `bool` for numbers and non-finite values
  are refused everywhere (spec compile, run start, CLI, dashboard).
- Types: `bool`, `int`, `float`, `str`, the nullable numbers `float | None` / `int | None`
  (`None` = unset; bounds apply only to numbers; the dashboard shows an "unset" checkbox),
  and `PolicyRef` (§3.2). `str | None` is not supported.
- Heavy dependencies: import inside `decide`, name them in `requires=`.
- Subclassing `PairSolver` (`policies/dispatch/pair_solver.py`) gives the legacy
  `solve(trucks, orders, *, pair_allowed, cost)` contract and a default `decide`. The two
  built-in leaves live in `apps/container_logistics/assignment/solver/`.

## 3. Write a decomposition

### 3.1 Use the built-ins first

`Partitioned(inner=X)` and `PooledMarket(offer, claim, arbitration, max_rounds, inner=X)`
take any leaf. Write a composite only when the *way the tick is split or coordinated* is the
experiment.

### 3.2 Refs: `PolicyRef`, `ref()`, `leaf_only`

```python
from apps.container_logistics.policies.framework import PolicyRef, ref

inner: PolicyRef = param(ref("RandomAssignment"), kind="dispatch", leaf_only=True,
                         help="The per-scope solver each group is decided with.")
```

- `kind` is required on, and only allowed on, a `PolicyRef` field: `dispatch`, or a
  component kind `market.offer` / `market.claim` / `market.arbitration`.
- `leaf_only=True` refuses a composite target (message 2). Every dispatch `inner` sets it.
- A ref is validated against the **live** registry of its kind, the target's params
  recursively, with the full path in the error, e.g.
  `policies.dispatch.params.inner.params for GreedyNearest: 'dual_cycle_bonus_km' must be >= 0 (got -1.0).`
  (a per-run override reads `policy overrides.dispatch.params.inner.params for …`).
- A policy with a `kind="dispatch"` ref is a composite (`PolicySpec.is_composite`).
- In JSON a ref value is always `{type, params?}`; a compiled or stamped ref also carries
  `version` (ignored on read). Any other key is refused.

### 3.3 Components: offer, claim, arbitration

| Kind | Built-ins | Default | Params |
|---|---|---|---|
| `market.offer` | `OfferAll`, `OfferNone`, `OfferSpare` | `OfferAll` | `OfferSpare`: `keep_below_km: float \| None` (default `None` = no threshold), `only_when_short: bool` (default `True`) |
| `market.claim` | `ClaimAllPlanned`, `ClaimNone`, `ClaimIfGainExceeds` | `ClaimAllPlanned` | `ClaimIfGainExceeds`: `min_gain_km: float` (default 0.0) |
| `market.arbitration` | `LowestCost`, `OwnerFirst`, `HighestBenefit`, `RandomAward` | `LowestCost` | none |

Each built-in has a typed `Params` and `version="1"`. They stay in `OFFER_REGISTRY`,
`CLAIM_REGISTRY` and `ARBITRATION_REGISTRY`, read live through `framework/components.py`.
`PooledMarket` instantiates them only through `components.instantiate_component`, which is
**loud** on an unknown name (message 3). The old fail-soft getters (`get_offer_policy`,
`get_claim_policy`, `get_arbitration_rule`) still exist as library functions; no runtime path
calls them. `openride policy list market.offer` lists a kind.

### 3.4 The `compose` and `scopes` helpers

`policies/dispatch/compose.py` (any solver may use them):
- `cooperation_groups(ctx) -> list[GroupScope]` (`key`, `trucks`, `orders`): one group per
  multi-company component of the active structure, else one per haulier, in `Partitioned`'s
  exact order. Pure.
- `decide_in_scope(policy, node, sub_ctx) -> Plan | None`: `decide`, then `check_plan`
  against the sub-scope. A violation logs message 19, counts, returns `None`; any other
  exception propagates.
- `heads_to_pairs(plan, ctx)`; `ScopeDecider(policy, node, parent)` (the adapter
  `pooled_planner` calls per company).

Sub-contexts are built only by `policies/dispatch/scopes.py`:
`partition_context(*, policy, params, candidate_kind, trucks, orders, partition_key, rule,
settings, accounting_cost, clock, sample_rng, parent)` and `company_context(…, haulier_id, round, rule,
rng, …, prebuilt_pairs, parent)`. They inherit `world`, `tick_seed`, `execute`, `stats`,
`scope_log`, `capture` and `verified_heads` from `parent`; RNG, tiebreak and rebate book
come from the `policy` passed in. `sample_rng` is **required, with no default**: it is the
generator a sampled scope draws its truck cap from. Pass `ctx.rng`; `None` means "this
context's `rng`". Only `Partitioned` passes the `random` module (pre-v2 byte identity, and
it declares `module_rng` for that draw). **Do not build a context by hand**: the audit (§4) and
capture rely on `scopes.py`.

Worked composite (validated the same way at `f8b91b9`: `PASS all 67 checks passed (1 skipped)`,
the skip being `order_independent[inner=RandomAssignment]`; benched):

```python
from __future__ import annotations

from dataclasses import dataclass

from apps.container_logistics.policies.dispatch import compose, scopes
from apps.container_logistics.policies.dispatch.composite import CompositePolicy
from apps.container_logistics.policies.dispatch.plan import Plan
from apps.container_logistics.policies.framework import PolicyParams, PolicyRef, param, policy, ref


@policy(slot="dispatch", name="LargestGroupFirst", version="1",
        description="Partitioned, but the cooperation group with the most trucks commits first.",
        information_model="per_group")             # no module_rng: it draws only ctx.rng
class LargestGroupFirst(CompositePolicy):
    @dataclass(frozen=True)
    class Params(PolicyParams):
        inner: PolicyRef = param(ref("GreedyNearest"), kind="dispatch", leaf_only=True,
                                 help="The per-group solver.")

    def decide(self, ctx) -> Plan:
        self.inner.set_rng(ctx.rng)               # the tick Random, never the random module
        self.inner.set_tiebreak(ctx.tick_seed)
        groups = sorted(compose.cooperation_groups(ctx), key=lambda g: (-len(g.trucks), str(g.key)))
        chains: dict[str, tuple[str, ...]] = {}
        for group in groups:
            sub = scopes.partition_context(
                policy=self.inner, params=self.inner_node.params,
                candidate_kind=self.inner_node.candidate_kind,
                trucks=list(group.trucks), orders=list(group.orders), partition_key=group.key,
                rule=ctx.world.rule, settings=ctx.candidate_settings,
                accounting_cost=ctx.accounting_cost, clock=ctx.clock,
                sample_rng=ctx.rng, parent=ctx)  # never the random module
            plan = ctx.execute(compose.decide_in_scope, self.inner, self.inner_node, sub)
            if plan is not None:                  # None: that group's plan was discarded
                chains.update(plan.chains)
        return Plan(chains=chains)
```

- `CompositePolicy` (`dispatch/composite.py`) is built as `cls(params, inner=, inner_node=)`,
  holds `inner` (the built leaf) and `inner_node` (its `ResolvedDispatch`), and forwards
  `set_rng`, `set_tiebreak` and `set_rebate_book` to `inner`. `innermost_policy(p)` walks to
  the leaf.
- **Randomness:** inject the tick's randomness into the inner once per tick
  (`self.inner.set_rng(ctx.rng)` and `self.inner.set_tiebreak(ctx.tick_seed)`) and pass
  `sample_rng=ctx.rng` to `partition_context`. All three are needed: without `set_tiebreak`
  a `GreedyNearest` inner falls back to a `self.rng.shuffle` tiebreak and the composite fails
  `determinism.order_independent`. The composite then passes
  `determinism.no_module_rng` on every arm with no declaration (the default arm and
  `[inner=RandomAssignment]` are real PASSes above). Passing `sample_rng=random` instead fails
  `determinism.no_module_rng[inner=RandomAssignment]` (checked: exit 1). This is the pattern
  `PooledMarket` uses.
- Run each heavy scope solve through `ctx.execute` (the eventlet `tpool` under
  `solve_in_thread`, which is off by default). The runner offloads a top-level **non-composite** itself; a composite
  is called directly and owns its scopes' offloading. Both built-ins do this: `Partitioned`
  per group, `PooledMarket` per company solve (`ScopeDecider.run` calls
  `company_ctx.execute`). Offload depth is 1: the runner never wraps a composite, so nothing
  is nested. With the flag off `execute` is a plain call. Exception: a composite-in-composite
  (an `inner` ref without `leaf_only=True`) offloads at depth 2, because the outer per-scope
  `ctx.execute` runs a composite that offloads again. Real `eventlet.tpool` runs a nested
  call directly, so there is no deadlock and pairs and RNG state are identical, but do not
  reason about hub latency from the depth-1 picture. Declare `leaf_only=True` unless the
  composite really composes composites.
- Notes keys `market` and `pair_notes` are reserved (§6). A third-party composite that sets
  `market` triggers the agent-side `meta.market` writer (doc 21 trap 6).

## 4. `@policy` arguments

| Argument | Required | Meaning |
|---|---|---|
| `slot`, `name`, `version`, `description` | yes | Bump `version` when output changes for the same input (§12). |
| `information_model` | **yes, for `dispatch`** | `central`, `per_group` or `per_company` (message 16). |
| `share_tag_format` | no, default `"audited"` | `"v1"` is reserved to `Partitioned` (message 15). Leave it out. |
| `module_rng` | no | A stated reason the decision draws the `random` module. Covers only this node's own draws (and, on a composite, its scopes' `sample_rng` draws), reported `SKIP`; any other module draw still FAILS (§8). |
| `order_sensitive` | no | A stated reason output depends on input order; `order_independent` is reported `SKIP`. |
| `requires` | no | Optional imports; missing → catalog "unavailable", selection refused. |
| `perf_budget_ms` | no, default 2000 | Battery budget for the **default** arm (§9). |
| `arm_budgets_ms` | no | Budgets for other arms, keyed `"param=value"` (`{"candidates=sampled": 4000}`). Unknown keys are refused at registration (message 17); ref-arm keys (`"inner=GreedyNearest"`) are checked by `tests/test_policy_arm_budgets.py`. |

**Information model.** The effective model is the **top-level** policy's:
`Partitioned/GreedyNearest` is `per_group`, `PooledMarket/GreedyNearest` `per_company`,
top-level `GreedyNearest` `central`. Stamped as `meta.policies.dispatch.information_model`,
shown in the catalog and badged in the dashboard. Enforcement is **structural**: sub-scopes
built by `scopes.py` hold only one group's, or one company's plus the market-visible,
data; the battery's `information.scopes_respect_model` audits every sub-scope via
`ctx.scope_log`. It certifies the helpers and the declared shape. **It cannot prove a
third-party solver does not peek** (e.g. by keeping a reference to the tick context).
The model describes what the **inner leaf** is handed. The decomposition itself sees the whole
tick, and inside `PooledMarket` the `claim` component gets the market object and arbitration
sees every bid.

## 5. The context (`DispatchContext`, `dispatch/context.py`)

One class for every scope. `ctx.scope.kind` is `tick`, `partition` or `company`;
`ctx.scope.key` is `tick`, `partition:haulier:<id>`, `partition:component:<n>` or
`company:<hid>`; `ctx.scope.round` is the market round (else `None`).

- `ctx.trucks` / `ctx.orders`: read-only `TruckView` / `OrderView` (`id`, `haulier_id`,
  `size`, `position`/`pickup`/`dropoff` as `(lon, lat)`, `last_dropoff_position`,
  `last_dropoff_facility`, facility names, `.raw`). `ctx.raw_trucks` / `ctx.raw_orders`: the
  documents. Never mutate.
- `ctx.allowed(t, o)`: feasibility **in this scope**. On the tick scope it is the world rule;
  on a company scope that company's rule (own orders plus market-visible ones).
- `ctx.candidate_pairs(kind=None)` (`spatial` / `sampled` / `full`; returns allowed pairs
  only), `ctx.sampled_trucks()`, `ctx.deadhead_km(t, o)` (haversine, `inf` without
  geometry), `ctx.dual_cycle_cost(t, o, bonus_km=, radius_km=)`, `ctx.params`, `ctx.clock`
  (`sim_clock`, `time_step`, `horizon_steps`), `ctx.rebates`, `ctx.accounting_cost`.
- `ctx.rng`, `ctx.tiebreak_seed`, `ctx.tick_seed` (§8); `ctx.execute(fn, *args)`;
  `ctx.stats` (shared with the runner: `calls`, `violations`, `total_ms`, `max_ms`,
  `crashes`); `ctx.id_index` (`(trucks_by_id, orders_by_id)`, first document per id).
- `ctx.world` (`DispatchWorld`, `dispatch/world.py`, framework-owned, read-only):
  `allowed`, `structure_allows`, `own_allowed`, `rule`, `accounting_cost`,
  `accounting_params`, `partners(hid)`, `may_serve(server, owner)`, `component_of`,
  `structure`, `max_travel_time_pickup`, `cooperation_pairs(hauliers)` (includes
  self-loops; what a central MIP needs). `None` on an offline parent-less sub-scope.

**The documents are read-only.** `ctx.raw_trucks`, `ctx.raw_orders` and every pair
`ctx.candidate_pairs()` yields are the tick's own dicts, shared by every scope of the tick.
Never mutate them, not even temporarily (for example, moving a truck to a pickup for a
sub-solve and restoring it afterwards). The framework's tick check trusts a sub-scope's
physical feasibility verdict for the heads that sub-scope already proved (keyed on document
identity) and re-checks only who may serve whom, so a head proved against a mutated document
is committed as proved. If a policy needs modified documents, build copies and map their
`_id`s back. This is a contract, not a check: verifying it per head measured ~0.9 ms per
tick, more than the shortcut saves.

**Tick scope vs sub-scopes.**
- Tick: spatial pairs over all trucks × all orders under the world rule. **Sampling caps
  `max_trucks_per_scope` PER HAULIER** (`TICK_SAMPLE_SCOPE = "per_haulier"`), drawing from
  `ctx.rng`. A partition or company scope caps its whole slice once. So under cooperation a
  sampled central leaf sees more candidates than `Partitioned` (golden chain tick at cap 40:
  160 vs 80); the stamp records `candidate_settings.sample_scope`.
- Partition scopes build spatial pairs on demand; sampling draws the `sample_rng` the
  composite passed (`Partitioned`: the `random` module; your composite: `ctx.rng`).
- A company scope has spatial pairs only when the resolved kind is `spatial` (prebuilt once
  per round). `candidate_pairs("spatial")` on a company scope of another kind raises
  `DispatchContractViolation` (logged, that scope discarded) rather than returning `[]`.

**Not available yet (P4):** `ctx.travel`, facility ids, order time windows, `ctx.state`,
`held` plan tails.

## 6. What the framework owns, and what happens to a bad plan

- **Ownership** (an order's haulier never changes), **feasibility** (`world.allowed` re-checked
  on every committed head, whatever the scope; a central policy pairing a truck with an order
  its company may not serve is caught here), **invariants** (`invariants.check_plan`: no
  truck/order twice, in-scope ids only, non-empty tuple chains, every head allowed),
  **commit** (heads only under `online`), **accounting** (§7), **`meta.market`** (only when
  the plan's notes carry `market`).
- `Plan(chains={truck_id: (order_id, ...)}, notes={...})`; `Plan.from_pairs(pairs, notes=)`
  builds a heads-only plan (a repeated truck raises). Insertion order is commit order.
  Deferring an order = leaving it out (it stays owned and unassigned). Reserved notes:
  `market` (`rounds_used`, `converged`, `candidate_pairs_round1`, `candidate_pairs_total`,
  `max_rounds`) and `pair_notes` (`{(truck_id, order_id): {...}}`, cross-company awards only;
  only `pool_id` and `market_round` reach a tag).
- A `PooledMarket` still committing when `max_rounds` is reached stops unconverged: the tick
  is counted as truncated (WARNING per tick, `POOLED MARKET TRUNCATED BY BACKSTOP` ERROR once
  per run, `meta.market.ticks_truncated_by_backstop`).

**Discards.** ERROR lines, agent-side: `journalctl --user -u openride-celery`, **not** `sim.log`.
- **Tick level** (message 18; whole tick discarded, nothing committed):
  `DISPATCH POLICY CONTRACT VIOLATION: <label> v<version> at step <n> returned an invalid tick plan: <problems>. The plan was DISCARDED (nothing committed this tick). This is a policy bug: run \`openride policy validate dispatch <name>\`.`
- **Scope level** (message 19; that scope only, others commit). Inside `PooledMarket` the
  scope is one company in one round: that company commits nothing that round.
  `DISPATCH POLICY CONTRACT VIOLATION: inner <name> v<version> in scope <key> at step <n> returned an invalid plan: <problems>. The scope's plan was DISCARDED (nothing committed for this scope this tick). This is a policy bug: run \`openride policy validate dispatch <name>\`.`

**Crashes (non-contract exceptions).**

| Where | What happens |
|---|---|
| Top-level leaf, `Partitioned`'s inner, your own composite (unless it catches), or a `PooledMarket` arbitration rule | Propagates out of `AssignmentApp.assign()`, i.e. the tick. ORSim's step handler catches it (`Exception in step for Agent …`): that tick commits nothing and the agent lives on. |
| `PooledMarket`'s inner | Message 20, `DISPATCH POLICY CRASHED: inner <name> raised for company '<hid>' in round <r> at step <n>; that company commits nothing this round (pooled-market behaviour, unchanged). Traceback follows. (celery journal, not sim.log)`; counted in `stats["crashes"]`; the tick continues. |
| `PooledMarket`'s offer or claim component | Swallowed: `Offer policy failed for haulier …` / `Claim policy failed for haulier …`; that company contributes / bids nothing. Not counted, not message 20. |

Violations and crashes are **not stamped into `run_config`**; only the journal line and the
in-memory `stats`. A run whose policy never commits anything can still finish `ok: true`.

## 7. Accounting is not your objective

Computed in `assignment/accounting.py` after the commit. The numbers come from the committed
pairs and the world cost (the only solver-supplied values are `pool_id`/`market_round` from
`pair_notes`), so the same committed pairs get the same tags whichever solver produced them:
`account_shares(pairs, *, candidates, cost, own_allowed)` then
`render_share_tags(accounts, *, fmt, pair_notes=None)` (called in
`AssignmentApp._dispatch_tick` with `cost=world.accounting_cost`,
`own_allowed=world.own_allowed`, `fmt` = the top policy's `share_tag_format`).

- Cross-company pairs only (both hauliers known and different).
- `owner_reserve_km`: the owner's cheapest free, own-feasible truck after the commit (`None`
  when it had none), so it depends on the plan;
  `awarded_cost_km = float(accounting_cost(t, o))` (exception → `inf`);
  `benefit_km = reserve − awarded`, **signed**, `None` when there is no reserve, rounded to
  0.001 km.
- The cost is `constraints.assignment_cost` with the profile's `solver_params`, whatever the
  policy optimises. `solver_params` mirrors the dual-cycle params of the first node, pre-order,
  that declares them (normally the leaf); stamped as `accounting_params`.
- The market's bids are priced with the same accounting cost; a future claim pricing
  differently would still be **accounted** at the framework cost (intended).

Two wire formats, the same values:
- `audited` (default): `shared`, `owner_haulier_id`, `carrier_haulier_id`, `benefit_km`,
  `pool_id`, `awarded_cost_km`, `owner_reserve_km`, `market_round`; non-finite → `None`
  (FIX-5). `pool_id`/`market_round` are `None` unless `pair_notes` supply them.
- `v1` (`Partitioned` only): the first four keys; a non-finite benefit stays `-inf` (the only
  reachable one: the reserve is always finite). Kept so
  every pre-v2 partitioned run stays byte-identical. Do not "clean it up".

## 8. Determinism

Draw randomness **only from `ctx.rng`** (or `self.rng` inside `with self.decision_rng(ctx):`).
- On the tick scope `ctx.rng` is `random.Random(tick_seed)`, `tick_seed = blake2b(run_id |
  time_step)`, built lazily on first read (the partitioned default never builds it). A
  sub-scope's RNG is the `policy.rng` its builder is given.
- `DispatchPolicy.decision_rng(ctx)` (a context-manager **method**, `dispatch/base.py`) binds
  `self.rng` / `self.tiebreak_seed` to the context for one decision and restores them in
  `finally`; `PairSolver.decide` and `GreedyNearest.decide` use it.
- Break ties with `ctx.tiebreak_seed` (`assignment/tiebreak.pair_tiebreak`) or a stable key
  (the example uses ids), or declare `order_sensitive="why"`.
- **Module RNG only where declared.** One extra module draw shifts trucks'
  `random() <= response_rate` rolls for the rest of the worker's life. `Partitioned` declares
  `module_rng` (pre-v2 byte identity); central leaves draw none (a test).
- `order_sensitive` composes: any node of the selection declaring it makes
  `order_independent` a **SKIP** for that arm.
- `module_rng` is **scoped to the node that declares it**. The battery gives each built node
  that would draw the module by default a private `Random(seed)`, wraps the `random` module's
  draw functions for one probe decision per fixture, and attributes every module draw to the
  nearest selection node on the stack (a draw made while building a sub-scope counts as that
  scope's `sample_rng`, but only for a scope handed to the inner; a scope a node builds for
  itself and samples from is that node's own draw). The owner of a draw is the nearest frame
  whose FIRST parameter is a policy of the selection, whatever that parameter is called; a
  thread a node starts during the decision (including a `ThreadPoolExecutor` it submits to)
  is that node's. A draw is covered only when its own node, or for a scope sample an
  ancestor, declares `module_rng`. Any uncovered draw **FAILS** naming the node (so
  `Partitioned[inner=ModuleRandom]` fails); covered draws give **SKIP**; no module change
  gives PASS. `Partitioned`'s default arm is a SKIP, `Partitioned[inner=GreedyNearest]` a PASS.

Battery limits: the attribution sees calls through the `random` module's attributes; a
function bound at import (`from random import random`), `random._inst`, or a thread that
existed before the decision still changes the state, and when nothing else drew that is an
unseparable SKIP ("provenance cannot be separated"), never PASS. Without a declaration `determinism.no_module_rng` compares
`random.getstate()` across the decision.
`determinism.order_independent` holds only while the spatial per-order cap
(`spatial_candidates_per_order`, default 40) does not bind.

## 9. Validate, bench, capture

```
openride policy validate dispatch RegretMatch           # PASS/FAIL table, exit 1 on any FAIL
openride policy bench dispatch --policies RegretMatch,part=Partitioned,pm=PooledMarket \
    --policy-param part.inner=RegretMatch --policy-param pm.inner=RegretMatch \
    --synthetic three_company_chain,scale [--json]
openride run <slug> --capture-dispatch every=10,max=40  # <datahub>/fixtures/dispatch/<run_id>/step_<n>.json.gz
openride policy bench dispatch --policies Partitioned,RegretMatch --fixtures <datahub>/fixtures/dispatch/<run_id>
```

Control-plane equivalents: `python -m openride_control.command policy-validate --slot dispatch
--name X` and `policy-bench --slot dispatch --policies … (--synthetic … | --fixtures DIR)`.
`python -m openride_control.command validate-run-policies --scenario S` with no policy flags
returns `{}` without reading the bundle, so it does not catch a bundle compiled before round
2; the run itself refuses such a bundle at start (exit 2, "compiled before round 2 …
recompile").

**Battery.** Ids: `structure.*` (returns_plan, no_truck_twice, no_order_twice, only_in_scope,
only_allowed_pairs), `robustness.<fixture>` (empty_trucks, empty_orders, single_truck,
no_geometry, one_haulier, all_infeasible, tied_costs, three_company_chain, missing_haulier,
scale), `determinism.{reproducible,order_independent,no_module_rng}`,
`information.{model_declared,scopes_respect_model}`, `performance.scale`,
`params.{defaults_validate,rejects_unknown_key,rejects_out_of_bounds,rejects_bad_choice,rejects_bad_ref,schema_keywords_supported}`. Every decision goes through the runner path
the sim executes. Each row's status is `PASS`, `FAIL` or **`SKIP`** (a declared exemption;
`ok` stays true, so the exit code is 0 and the summary reads `N passed, k skipped`;
`all N checks passed` when nothing is skipped); `policy-validate` JSON carries `status`/`skipped` per row and a `skipped`
count. `tests/test_policy_contract_battery.py` runs it over every registered policy.

**Arms.** Besides the default, each is checked separately with the arm in the id
(`performance.scale[candidates=sampled]`, `robustness.scale[inner=GreedyNearest]`): every
non-default `choices` value of every `str` param, and for each ref param every other
registered target of its kind satisfying `leaf_only`, at its defaults. Not crossed, not
recursive.

**Per-arm budgets.** `arm_budgets_ms.get(arm, perf_budget_ms)` on the 1000×500 `scale`
fixture, best of 3. Rule: `max(3 × worst of 2×5 runs, 500)` ms, measurements written beside
the decorator (this box varies 1.3–2× on its own). `battery.arm_budget(spec, arm)` decides:
a measured `arm_budgets_ms` key always wins; an unmeasured composite `inner=X` arm gets
**X's own `perf_budget_ms` plus the composite's default `perf_budget_ms`** (the detail names
the derivation); any other unmeasured arm inherits the default's budget and fails loudly if
slower. A composite that calls its inner many times on the one-company `scale` tick must
measure its `inner=` arms. Current: `GreedyNearest` 500 / `candidates=sampled` 5790;
`RandomAssignment` 550; `Partitioned` 660 / `inner=GreedyNearest` 500; `PooledMarket` 500,
with 500–620 per arm. A budget above 2000 ms (`STEP_SETTLE_SECONDS`) means that arm makes
the assignment agent a per-step straggler at 1000×500 — `GreedyNearest candidates=sampled`
is one.

**Bench columns:** `policy, label, info_model, fixtures, assigned, orders, assign_rate,
deadhead_km_total, deadhead_km_mean, dual_cycle, cross_company, benefit_km_total,
solve_ms_p50, solve_ms_max, violations`. `benefit_km_total` comes from the framework's
`account_shares`. Deadhead is **haversine at decision time**, not the road-distance KPI.
Single ticks screen; runs certify (n≥3, interleaved).

**Capture.** Format `openride.dispatch_context/2`, one file per captured tick
(`step_<n>.json.gz`): `run_id`, `time_step`, `sim_clock`, `horizon_steps`, `tick_seed`,
`module_rng_state`, `selection` (stamp form), `candidate_settings`, `world`
(`structure`, `max_travel_time_pickup`, `accounting_params`), `trucks`, `orders`,
`live_heads`, `rebate_aware`. `every=N` counts **dispatch ticks, not sim steps**: the first
tick is always captured, then every N-th (a runner rebuilt mid-run restarts the count;
`max` still counts the files on disk). Replay reproduces the live heads; it refuses a rebate-aware tick
(the rebate book is never captured, so re-capturing does not help) and any `/1` file
(message 22: re-capture). Also `run-simulation --capture-dispatch` and
`ORSIM_CAPTURE_DISPATCH=every=N,max=M`; a malformed value refuses the run (exit 2).

## 10. Select it

**Scenario (`spec.json`).** `policies.dispatch` is the only dial:

```json
"policies": {"dispatch": {"type": "PooledMarket", "timing": "online",
  "params": {"max_rounds": 20,
             "offer": {"type": "OfferSpare", "params": {"keep_below_km": 3.0}},
             "claim": {"type": "ClaimAllPlanned"}, "arbitration": {"type": "LowestCost"},
             "inner": {"type": "GreedyNearest", "params": {"dual_cycle_bonus_km": 5.0}}}}}
```

Slot default: `Partitioned{inner: RandomAssignment}`. Compile writes the compiled profile's
`policies` block **plus `policies_format: 2`** and the legacy mirrors (§11). The written
`spec.json` carries only the `policies` slots its author wrote: a legacy spec keeps its
`solver`/`planner` keys and gains no derived block (the bundle's `$.recipe` still carries the
full block). The dashboard's **Operator
policies** card writes the same block (ref params as a dropdown + nested form, info-model
badges); the Run step's **structure** dropdown is separate and unchanged.

CLI on a scenario: `openride scenario new --solver X` → `Partitioned{inner: X}` (never a flat
central `{type: X}`); `openride scenario edit <slug> --solver X` sets the **innermost leaf** of
the current selection; `--policy dispatch=X` / `--policy-param …` rebase onto the current
block.

**One run** (`openride run`, `run-simulation`, `validate-run-policies`):
- `--policy dispatch=PooledMarket` sets the top type. A changed type starts from its defaults,
  **except that a new decomposition keeps the current per-scope solver as its `inner`, with
  that solver's params** (the current `inner`, or the current top when that is a leaf),
  unless the request names the inner's type (spec §3.3).
- `--policy-param dispatch.inner=RandomAssignment` sets a ref's type (params reset);
  `--policy-param dispatch.inner.dual_cycle_bonus_km=0`, `dispatch.offer.keep_below_km=3`,
  `dispatch.max_rounds=5`: nested paths (slot = longest known-slot prefix; every other
  non-final segment is a ref). VALUE is JSON when it parses.
- `--solver X` (`ORSIM_SOLVER`, shipped **separately** from `ORSIM_POLICY_OVERRIDES`) sets the
  innermost leaf (or a leaf top). A decomposition name is refused (message 11), a different
  explicit `dispatch.inner.type` too (message 12, which names where it came from:
  `--policy/--policy-param/--policy-overrides`). The source profile's `policies_format`
  marker is checked **before** any override resolves, so no override (not even `--solver`)
  can make an unmarked round-1 block runnable.
- Order of application (`scenario_overrides.run_dispatch_overrides`, shared by the sim and the
  dry run): overrides → `ORSIM_PLANNER_TOPOLOGY` → market env → `--solver`.
- `validate-run-policies --scenario S [--solver X] [--policy …] [--policy-param …]
  [--policy-overrides JSON]` is the synchronous dry run: `{ok: true, action:
  "validate_run_policies", overrides}` (the `ORSIM_POLICY_OVERRIDES` object, without the
  solver) or `{ok: false, action, message}`. Names are checked before the bundle is read, so
  `--scenario nope --solver Nope` names `Nope`. The dashboard maps `ok:false` to HTTP 400 and
  a non-answer (older backend) to 503.

**Trap.** `--policy dispatch=GreedyNearest` selects a **central** planner (label
`GreedyNearest`, `central`), a new arm. For "same decomposition, other leaf" use `--solver X`
or `--policy-param dispatch.inner=X`.

**Run-name segment** (`dispatch_override_label`): legacy forms are byte-identical
(`--solver GreedyNearest` → ` · GreedyNearest`; `ORSIM_PLANNER_TOPOLOGY` → ` · pooled` at its
old position, the market env vars add none). Only the leaf touched → ` · Leaf` or
` · Leaf(k=v)` (values as compact JSON: `GreedyNearest(candidates="sampled")`). A higher node
touched → the slash chain, ` · PooledMarket/GreedyNearest`,
` · PooledMarket(max_rounds=5)/GreedyNearest`. **Known limit:** central
`--policy dispatch=GreedyNearest` and `--solver GreedyNearest` both read ` · GreedyNearest`;
tell them apart by the stamp (label `GreedyNearest`/`central` vs
`Partitioned/GreedyNearest`/`per_group`). A ` · central:<Leaf>` prefix was proposed and is
**not implemented** (it would change the format `lib/runNameMeta.ts` parses).

**The stamp** `run_config.meta.policies.dispatch`: `name`, `version`, `kind` (the slot kind,
`operator`), `timing`, `source` (`bundle` / `legacy-profile` / `override`), `label`, `leaf`,
`information_model`, `share_tag_format`, `params` (nested, refs `{type, version, params}`),
`candidate_settings` (+ `sample_scope`: `per_haulier` for a central top, `per_group`,
`per_company`), `accounting_params`. Console:
`Policies effective: dispatch=<label> v<version> timing=<t> information_model=<m> params=<json> accounting_params=<json>`.

## 11. Legacy spellings (translated deterministically, `policies/dispatch/legacy.py`)

| Legacy input | Becomes |
|---|---|
| `solver` / `planner.deployment` / `solverParams`, topology absent or `partitioned` | `Partitioned{inner: S}` |
| same, `planner.topology` `pooled` or `two-stage` | `PooledMarket{offer, claim, arbitration, max_rounds from planner.market (defaults OfferAll / ClaimAllPlanned / LowestCost / 20), inner: S}` |
| bundle profile without `policies` (`strategy`, `solver_params`, `use_spatial_matching`, `planner`) | same; `use_spatial_matching: false` → `inner.candidates = "sampled"` where declared; no `strategy` → `RandomAssignment`. Accepted without `policies_format` |
| undeclared leaf/component params | dropped at WARNING |
| unknown topology (message 6), unknown component (3), `max_rounds` not an int in 1..100 | **loud** (used to fail soft / clamp) |
| `ORSIM_PLANNER_TOPOLOGY=pooled\|two-stage\|partitioned` | top → PooledMarket / Partitioned, inner kept; market from the current PooledMarket else `profile.planner.market`; conflict with an explicit `dispatch.type` refused (10) |
| `ORSIM_OFFER_POLICY` / `_CLAIM_POLICY` / `_ARBITRATION_RULE` / `_MARKET_MAX_ROUNDS` | the PooledMarket component (params reset) / `max_rounds` 1..100 (11..100 used to be ignored); refused unless the top is PooledMarket after the topology step (9) |
| a `profile.policies` block **without `policies_format: 2`** (round-1 compile on this branch) | **refused at run start**, checked on the source profile before any override applies: recompile (`openride scenario compile <slug>`), never hand-patch |

Next to an authored `policies` block: `solver`/`solverParams` must equal the innermost leaf;
`planner.topology` must equal the echo (Partitioned ↔ `partitioned`, PooledMarket ↔ `pooled`;
any value is an error for another top, message 7); `planner.market` must agree with
PooledMarket's params (8) and is inert next to another top. At run start the compiled
`policies` block is authoritative; the mirrors (`strategy`, `solver_params`,
`planner.deployment.params`, `planner.topology`, `planner.market`) are checked, not read
(`profile.check_mirrors`, message 13), and pools must agree with adjacency
(`world.validate_cooperation_world`, 14).

## 12. Versioning, comparability, restarts

- **Bump `version=`** when output changes for the same input, then
  `venv/bin/python tests/golden/capture_policy_golden.py --policy <Name>` (replaces that entry
  of `tests/golden/policy_golden_v2.json.gz`). A different `inner` is a selection, not a version.
- Every existing scenario at its defaults is byte-identical to pre-v2 (scope: doc 26 §5). A
  central top-level leaf, a `PooledMarket` with another `inner`, `RandomAssignment
  candidates=spatial`, or any param change is a **new arm, not comparable to old runs**; read
  the arm from `label` and `information_model`. Noise is ~4–5% on `empty_ratio`: n≥3.
- **Adding or editing a file under `apps/container_logistics/policies/**`** (or the solvers,
  components, `accounting.py`, `pooled_planner.py`) needs `systemctl --user restart
  openride-celery`. **Selecting a policy or changing its params never does** (data at spawn).
- A compiled `profile.policies` must carry `policies_format: 2`, the integer. An absent
  marker is refused as "compiled before round 2"; any other value (`2.0`, `"2"`, `true`,
  `3`) as an "unknown format". In both cases the fix is `openride scenario compile <slug>`;
  never hand-patch a bundle.

## 13. Known limits

- The information model is a tripwire, not a proof.
- No nested decompositions; no per-company inner (`inner_by_company` needs a map-of-refs, P5).
- Only `online` timing executes; `plan` timing with held tails is P4.
- The battery's module-RNG attribution cannot separate a draw through a function bound at
  import (reported SKIP, §8).
- Central `--policy dispatch=X` and `--solver X` give the same run-name segment (§10).
- Central and partitioned arms differ in sampled candidate coverage under cooperation (§5).
- Violations and crashes are not in `run_config`.
- Capture /2 refuses rebate-aware ticks.
