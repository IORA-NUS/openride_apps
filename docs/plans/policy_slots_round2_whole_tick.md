# Policy slots, round 2: the dispatch policy gets the whole tick

**Planner spec for the coder.** Worktree `/home/user/openride_apps_policy_slots` (branch `feat/policy-slots`, HEAD `c30fd25`, base `ec085f5`). FE means `/home/user/openride_server/analytics` branch `feat/policy-slots` at `03607b9`; read it with `git show` only, never `checkout`. I changed no files.

Short version. The framework hands one whole-tick `DispatchContext` to one policy and gets one `Plan` back.
- **The framework owns:** ownership, cooperation feasibility (`ctx.allowed`), the invariants, committing, and the accounting.
- **The solvers own the decomposition.** Today's two topologies become ordinary built-in solvers:
  - `Partitioned(inner=…)`
  - `PooledMarket(offer, claim, arbitration, max_rounds, inner=…)`
- **Leaves:** `GreedyNearest` and `RandomAssignment` work on any context they are given. Selected at top level, they are central planners.
- **Removed:** `market.offer` and `market.claim` stop being slots. They become *components* that `PooledMarket` references.
- **One new concept:** a *policy-reference param*. It expresses nesting in the spec, the CLI, the stamp, the schema and the form.
- **Accounting:** one framework function computes every share tag's values for every solver. Two historical wire formats are rendered from the same values. The old format, `v1`, is reserved for `Partitioned` so the golden stays byte-identical.

---

## 1. Verified current state (file:line, worktree HEAD)

### 1.1 `assignment/app.py`

| # | Fact | Where |
|---|---|---|
| S1 | `assign()` order of operations: reset `_share_tags`; `prof`; `_inject_rebate_book`; read trucks, orders, claimed and busy; `filter_assignable_trucks` → `candidates`; branch on `_resolved_topology(prof) == "pooled"`. | 567-613 |
| S2 | Partitioned path, setup. It builds `structure`, `adjacency = share_adjacency(structure)` and `component_of` (only when `adjacency and isinstance(structure, dict)`, members of components with length > 1). It builds `rule = PartitionRule(mtt, adjacency)` and `_cost = assignment_cost(t, o, self._solver_params)`. | 615-650 |
| S2a | Partitioned path, solving. Partition key is `("component", idx)`, else `("haulier", hid)`, else `None`. `truck_buckets` and `order_buckets` are filled in input order. It iterates `order_buckets.items()`, skips the `None` key and empty truck buckets, and solves per bucket through `runner.run(runner.partition_context(...))`, optionally inside `_eventlet_tpool.execute`. | 652-706 |
| S3 | Partitioned `benefit_km`, computed per bucket after its solve. For each pair whose truck and order hauliers both exist and differ: `own_costs = [_cost(t, order) for t in bucket_trucks if id not in used (this bucket's result) and haulier_of(t) == owner and rule.allowed(t, order)]`, then `best = min(c != inf)`. The tag is `{shared, owner_haulier_id, carrier_haulier_id, benefit_km: round(best - _cost(truck, order), 3) or None}`: 4 keys only, and `-inf` when the chosen cost is `inf`. | 707-737 |
| S4 | Pooled path, setup. It computes `tick_seed = blake2b(f"{run_id}\|{time_step}")` and builds one `rng = random.Random(tick_seed)`. It calls `self._solver.set_rng(rng)` and `self._solver.set_tiebreak(tick_seed)`, then builds `PoolMarket.from_structure(structure)`. | 495-512 |
| S4a | Pooled path, run. `trucks_by_haulier` and `orders_by_haulier` skip a `None` haulier. It reads `offer, claim, arbitration, max_rounds = _market_components(prof)`, which calls `resolve_market_config` (fail-soft registries, clamped rounds). It calls `run_pooled_market`, then `_record_round_stats(result, max_rounds, time_step)` and `_patch_round_stats_to_run_config()` (the agent-side `meta.market` writer). | 514-556 |
| S5 | `publish()` reads each tag with `tag.get` into `AssignedHaulTripPayload` and calls `json.dumps(payload.__dict__, default=str)`. | 762-789 |
| S6 | `_resolved_topology` is fail-soft: unknown values become `partitioned`. Topology is read on every call. | 172-184 |
| S6a | `_market_components` caches on the resolved market JSON. | 266-294 |

### 1.2 `assignment/pooled_planner.py` (the market)

| # | Fact | Where |
|---|---|---|
| S7 | `owner_reserve_km` takes the minimum over the owner's trucks that are not committed, pass `pair_allowed_own`, and have a finite float cost. | 174-210 |
| S7a | Contribution, then rounds that end on convergence. Companies are iterated `sorted`. `build_round_candidates` is called through the module global. `_solve_company` keeps its early returns. A company exception is logged as "Deployment solve failed" and that company is skipped. | 406-643 |
| S7b | The step-6 share tag has 8 keys. `awarded_cost_km` comes from `_finite_or_none(award.cost_km)`. `benefit_km = round(_finite_or_none(reserve - awarded), 3)`. `pool_id` is `award.pool_id`; `market_round` is `award.round`. | 645-688 |
| S7c | The I-P1 to I-P3 asserts. | 690-703 |
| S8 | Every `award.cost_km` equals `float(cost(t, o))`, where `cost` is the app's accounting `_cost`: private commits via `float(ctx.cost(...))` (pooled_planner 586), bids via `claim._cost_km` (claim.py:69-73, which turns an exception into `inf`), and `_sweep` copies `bid.cost_km` (arbitration.py:108). So the framework can recompute `awarded_cost_km` from the accounting cost. | |
| S9 | `pair_allowed_pooled` is only a context field now; `CompanyRule` replaced it. | pooled_planner 164-165 |
| S10 | `OfferSpare.keep_below_km` defaults to `inf`. That value cannot be a `PolicyParams` value today, because non-finite values are refused. `ClaimIfGainExceeds.min_gain_km` defaults to 0.0, with no bound. | offer.py:184-193; claim.py:165 |
| S11 | Tests register policies directly into the live registries and expect them to resolve: `CLAIM_REGISTRY["_TestFabricatingClaim"]`. | test_assignment_pooled.py:272 |

### 1.3 RNG

| # | Fact | Where |
|---|---|---|
| S12 | Partitioned: the leaf's `self.rng` is the `random` module (never injected), and sampling also uses the module. | scopes.py:271-273 |
| S12a | Pooled: offer, claim, arbitration, company sampling and the leaf all draw from the single tick `Random`. | |
| S12b | `DispatchContext` sampling is lazy and draws at most once. | context.py:135-146 |

### 1.4 Constraints and pools

| # | Fact | Where |
|---|---|---|
| S13 | `share_eligible` uses edge-direct adjacency. `haulier_matches` fails closed on `None`. | constraints.py:218-228, 329-348 |
| S13a | `pools_from_structure`: authored pools win, otherwise one pool per edge. Compiled structures derive adjacency from the edges, and the edges from the pools, so the two always agree. Only a hand edit can make them disagree. | pools.py:101-140; hauliers.py:376-403 |

### 1.5 Golden harness and tests

| # | Fact | Where |
|---|---|---|
| S14 | `build_app` uses `__new__` with `_solver = get_solver(strategy, solver_params)` (a leaf) and a legacy profile. `run_case` reads `app._share_tags` and `app.round_stats()` (pooled only) and draws `rng_probe`. | tests/golden/harness.py:90-138 |
| S14a | The identity test compares 5 fields exactly. The via-`__init__` test asserts `type(app._solver) is GreedyNearestSolver`. | test_dispatch_golden_identity.py:66-76; test_dispatch_golden_via_init.py |
| S15 | Pre-existing tests that lock fail-soft or topology internals: `test_unknown_policy_names_are_fail_soft_and_still_assign`; the `_resolved_topology` tests; `_market_components` used directly; the fail-soft topology and market override tests. | test_assignment_pooled.py:486-517; test_pool_housekeeping.py:45-53; test_assignment_pooled_convergence.py:230-240; test_scenario_market_overrides.py:35-138 |
| S16 | `__new__` harnesses without `run_id`. | test_container_logistics_hauliers.py:121-131 |

### 1.6 The main tree's uncommitted Kafka work

| # | Fact |
|---|---|
| S17 | `git -C /home/user/openride_apps diff`, hunk 1, `app.py`: inserts `_award_sim_clock` / `_award_time_step` right after `self._share_tags = {}` at the top of `assign()`. |
| S17a | Hunk 2, `app.py`: adds `sim_clock=` and `time_step=` kwargs after `market_round=` in `publish()`. |
| S17b | The payload dataclass appends the two fields at the end. |
| S17c | Sim main: inserts `stamp_transport_settings` after the runtime-settings refresh. |

### 1.7 Selection, overrides and control

| # | Fact | Where |
|---|---|---|
| S18 | Sim main applies, in order: policy overrides → `· <dispatch>` label; capture; cooperation override → `· <structure>`; `ORSIM_PLANNER_TOPOLOGY` → `· <topology>` (fail-soft); `ORSIM_OFFER/CLAIM/ARBITRATION/MARKET_MAX_ROUNDS` (fail-soft; rounds limited to 1..10, else ignored); run-start check; policies stamp; COOPERATION stamp plus a structure segment if missing. | run_container_logistics_simulation.py:355-484 |
| S19 | `build_run_policy_overrides` folds `--solver` into `dispatch.type`. `validate_run_policy_args` loads the bundle before validating any name, so a bad name is reported as "no bundle". | openride_control/policies.py:110-119, 186-192 |
| S20 | `_normalize_policies` translates `solver`, `deployment` and `solverParams` into a flat `dispatch` entry and never looks at topology. Compile mirrors `strategy`, `solver_params` and `deployment`. | preprocess.py:274-342, 696-713 |
| S21 | Seven slots; `dispatch` default `RandomAssignment`; `market.offer` and `market.claim` have status `legacy`. | slots.py:65-123 |
| S22 | `STEP_SETTLE_SECONDS = 2`, `STEP_TIMEOUT = 30`. | runtime_settings.py:66-72 |
| S23 | NetworkFlow's inputs: orders (owner, locations, times), `fleet {company: count}`, `cooperation {(owner, server)}` including self-loops, and a `travel_lookup`. It assigns orders across companies centrally. | /home/user/solvers/dispatch_solvers/standalone/README.md |

---

## 2. The new architecture

```
AssignmentApp.assign(sim_clock, t)                         (FRAMEWORK, agent-side)
  read trucks / orders / claimed / busy → candidates        (unchanged lines)
  world  = DispatchWorld.from_profile(prof, accounting_params=_solver_params)
             · structure (active, post-override) · rule = PartitionRule(mtt, adjacency)
             · accounting_cost = assignment_cost(·,·,accounting_params) · own_allowed = pair_allowed(mtt)
  runner = _dispatch_runner(prof)        # resolved selection tree + bound instance (cached)
  ctx    = runner.tick_context(candidates, orders, world, settings, clock,
                               tick_seed=blake2b(run_id|t), execute=direct|tpool)
  out    = runner.run(ctx):  decide(ctx) → Plan → capture → check_plan(world.allowed!) → commit heads
  accounts = accounting.account_shares(out.pairs, candidates, world)   ← SOLVER-INDEPENDENT
  _share_tags = accounting.render_share_tags(accounts, fmt=runner.share_tag_format,
                                             pair_notes=out.notes.get("pair_notes"))
  if out.notes has "market": _record_round_stats / meta.market patch  (solver provenance)
  return out.pairs                         publish() unchanged

SOLVERS (policies/dispatch/library + assignment/solver), selected as a tree:
  Partitioned(inner)        per_group   compose.cooperation_groups → group_context → decide_in_scope(inner)
  PooledMarket(offer, claim, arbitration, max_rounds, inner)
                            per_company pooled_planner.run_pooled_market(..., ScopeDecider(inner))
  GreedyNearest / RandomAssignment  central (scope-agnostic leaves; inside a wrapper they decide one scope)
  NetworkFlow (P4)          central     decides the whole tick directly
```

### 2.1 The whole-tick `DispatchContext` (`dispatch/context.py`, one class for every scope)

The new fields:
- `scope`, whose kind is one of `tick`, `partition` or `company`.
- `world`, a read-only `DispatchWorld`.
- `tick_seed`.
- `rng`: a property. On the tick scope it is `random.Random(tick_seed)`, built lazily the first time it is read. A sub-scope is handed an explicit value.
- `tiebreak_seed`: `tick_seed` on the tick scope; on a sub-scope, the value its builder passes.
- `execute(fn, *args)`: a direct call, or `tpool` when `solve_in_thread` is set.
- `stats`: shared with the runner.
- `scope_log`: `None` unless auditing; the battery turns it on.
- `id_index`: a cached property holding `(trucks_by_id, orders_by_id)`, first document per id. `check_plan` and commit share it.

Unchanged fields: `params`, `clock`, `rebates`, `candidate_settings`, `default_candidate_kind`, `raw_trucks`, `raw_orders`, `allowed_raw`, `accounting_cost`, lazy `trucks` and `orders` views, `candidate_pairs`, `sampled_trucks`, `deadhead_km`, `dual_cycle_cost`.

Spatial pairs on the tick scope come from `spatial_pairs_for_partition(all trucks, all orders, rule=world.rule, settings)`. Sampling on the tick scope draws from `ctx.rng`.

Nothing is copied beyond `tuple(candidates)` and `tuple(orders)`, which the per-bucket code already did.

### 2.2 `DispatchWorld` (`dispatch/world.py`, framework)

- Methods: `rule`, `allowed(t, o)`, `own_allowed`, `accounting_cost`, `partners(hid)`, `may_serve(server, owner)`, `cooperation_pairs(hauliers)` (the `(owner, server)` pairs including self-loops; this is what NetworkFlow needs), `component_of`, `structure`.
- `validate_cooperation_world(profile)` runs at run start only. It raises `PolicyError` (message 14) when a pool pairs two companies that the adjacency does not.

### 2.3 Built-in solvers and reusable helpers

**`dispatch/compose.py`** (library, callable by any solver):
- `cooperation_groups(ctx) -> list[GroupScope]` is a verbatim move of S2.
- `decide_in_scope(policy, node, sub_ctx) -> Plan | None` does decide → `check_plan` against the sub-scope. On a violation it logs, counts and returns `None`.
- `heads_to_pairs(plan, ctx)`.
- `ScopeDecider(policy, node, parent_ctx)` is the adapter `pooled_planner` already calls: `candidate_kind`, `company_rule(hid, mtt)`, `company_context(**kw)`, `run(company_ctx)`.

**`dispatch/scopes.py`** remains the only place contexts are built: `tick_context`, `partition_context` (the group scope) and `company_context`. Each takes a `parent` context and inherits `world`, `tick_seed`, `execute`, `stats` and `scope_log` from it.

**`library/partitioned.py`** registers `Partitioned`:
- Declarations: `information_model="per_group"`, `share_tag_format="v1"`, `module_rng="…pre-v2 byte identity…"`, `order_sensitive="inner ties broken by a module-RNG shuffle over input order (pre-v2)"`.
- Sub-context values: `rng=self.inner.rng`, `tiebreak=self.inner.tiebreak_seed`, `sample_rng=random` (the module), spatial pairs from `spatial_pairs_for_partition(bucket, rule=world.rule)`.
- Each group is decided through `ctx.execute(decide_in_scope, …)`.
- It merges the group plans in group order. Inner exceptions propagate, as today.

**`library/pooled_market.py`** registers `PooledMarket` with `information_model="per_company"` and `share_tag_format="audited"`. `decide(ctx)`:
1. `self.inner.set_rng(ctx.rng)` and `self.inner.set_tiebreak(ctx.tick_seed)`, through the methods so the monkeypatch test still bites.
2. Build the offer, claim and arbitration components from the typed params.
3. Build `trucks_by_haulier` and `orders_by_haulier`, moved from S4a.
4. `PoolMarket.from_structure(ctx.world.structure)`.
5. `run_pooled_market(..., PooledPlannerContext(dispatch=ScopeDecider(...), cost=ctx.accounting_cost, rng=ctx.rng, tick_seed=ctx.tick_seed, ...))`.
6. Return `Plan(chains in award order, notes={"market": {rounds_used, converged, candidate_pairs_round1, candidate_pairs_total, max_rounds}, "pair_notes": {(tid, oid): {"pool_id", "market_round"}}})`.

The company exception path stays, with the log prefix `DISPATCH POLICY CRASHED` and a count in `stats`.

**`assignment/pooled_planner.py`** becomes the market *engine* on the solver side:
- Step 6 no longer computes tags. It returns `pair_notes`.
- `owner_reserve_km` and `_finite_or_none` move to `assignment/accounting.py`.
- `pair_allowed_own` and `pair_allowed_pooled` are dropped from the context (this closes F14).
- `build_round_candidates`, the constants, `resolve_market_config` and `effective_cooperation_stamp` stay where they are, so monkeypatch targets and imports keep working.

### 2.4 Information model

- `@policy(..., information_model=...)` takes `central`, `per_group` or `per_company`. It is **required** for slot `dispatch`.
- The effective model of a selection is the top-level policy's model. That rule is sound because an `inner` must be a leaf (`leaf_only` refs; a leaf is a policy with no dispatch ref).
- It is stamped as `run_config.meta.policies.dispatch.information_model`, shown in the catalog and the CLI, and badged in the form.
- **Enforcement is structural.** A company scope built by `compose`/`scopes` holds only that company's trucks plus the orders the market made visible. A group scope holds one cooperation group.
- The battery check `information.scopes_respect_model` audits `scope_log`:
  - per_company: every sub-scope's trucks belong to one haulier, and every order is owned by that haulier or a world partner.
  - per_group: trucks and orders all fall inside one cooperation group.
- Honest limit: this cannot *prove* a third-party solver does not peek. It certifies the helpers and the declared shape.

### 2.5 Accounting flow (`assignment/accounting.py`, framework, no solver input)

```python
@dataclass(frozen=True)
class ShareAccount:
    truck_id: str; order_id: str; owner_haulier_id: str; carrier_haulier_id: str
    owner_reserve_km: float | None    # owner_reserve_km(...) moved verbatim
    awarded_cost_km: float            # float(cost(t,o)); exception -> inf (same as claim._cost_km)

def account_shares(pairs, *, candidates, cost, own_allowed) -> list[ShareAccount]
def render_share_tags(accounts, *, fmt: Literal["v1", "audited"],
                      pair_notes: Mapping[tuple[str, str], Mapping] | None) -> dict[tuple[str, str], dict]
```

`account_shares`:
- Covers cross-company pairs only: both hauliers known and different.
- The reserve is computed over the owner's trucks in `candidates`, skipping any truck in the committed set.
- The owner index is built lazily, only when the first cross pair appears.

`render_share_tags`:
- `v1` gives `{shared, owner_haulier_id, carrier_haulier_id, benefit_km: round(reserve - awarded, 3) if reserve is not None else None}`.
- `audited` gives the 8 pooled keys. Values pass through `finite_or_none` exactly as S7b does. `pool_id` and `market_round` are read only from whitelisted `pair_notes` keys; they are `None` when absent.
- What the two formats share: the *values* (reserve, awarded cost, benefit) are computed by one framework function for every solver.
- What differs: `v1` omits the audit pair and renders a non-finite benefit as ±inf (the FIX-5 defect). It is registration-reserved to `Partitioned`, so pre-v2 partitioned runs stay byte-identical.
- The F1 one-dial mirror is preserved. `solver_params`, the accounting dials, mirrors the dual-cycle params of the first node in a pre-order walk of the selection tree that declares them; normally that is the inner leaf. The values are stamped as `accounting_params`.

### 2.6 How the market lives as a solver

- **The market keeps:** offers, rounds, claims, arbitration, I-P1 to I-P4, round provenance, and pricing bids with `ctx.accounting_cost` (today's behaviour; a market pricing with its inner's objective would be a new arm).
- **The framework does:** the cross-company feasibility re-check of every head (world rule), the accounting, and the `meta.market` patch. It still patches only when `notes["market"]` exists, so the multi-writer trap (doc 21 trap 6) stays on the pooled path only.

---

## 3. User-facing selection

### 3.1 Spec (the only dial; `strategy`, `solver_params`, `planner.deployment`, `planner.topology` and `planner.market` become mirrors or legacy)

```json
"policies": {"dispatch": {"type": "PooledMarket", "timing": "online",
  "params": {"max_rounds": 20,
             "offer": {"type": "OfferSpare", "params": {"keep_below_km": 3.0}},
             "claim": {"type": "ClaimAllPlanned"}, "arbitration": {"type": "LowestCost"},
             "inner": {"type": "GreedyNearest", "params": {"dual_cycle_bonus_km": 5.0}}}}}
```

Rules:
- **Slots:** `dispatch` (default `Partitioned`, whose `inner` defaults to `RandomAssignment`, which is the old no-strategy default), plus planned `idle`, `queue`, `world.shedding`, `world.duration`.
- **Ref values:** in JSON a ref value is always `{type, params?}`. A compiled or stamped ref also carries `version`, which is ignored on read.

### 3.2 The new `PolicyParams` types

- **`PolicyRef`:** declared as `inner: PolicyRef = param(ref("RandomAssignment"), kind="dispatch", leaf_only=True, help=...)`. It is validated against the *live* target registry: the policy registry for `dispatch`, or the existing `OFFER_REGISTRY`, `CLAIM_REGISTRY` and `ARBITRATION_REGISTRY` dicts through `framework/components.py`, so S11 keeps working. The target's params are validated recursively, with the path in the error message.
- **Nullable number:** `float | None` or `int | None`, where `None` means unset. Needed for `OfferSpare.keep_below_km` (S10). A `None` value is dropped before a legacy component class is instantiated, so it uses its own `inf` default.

Schema additions:
- A ref is `{"type": "object", "x-policy-ref": "<kind>", "x-leaf-only": bool, "default": {"type": X, "params": {...}}, "description": ...}`.
- A nullable number is `"type": ["number", "null"]`.
- Both `x-` keywords join `SUPPORTED_SCHEMA_KEYWORDS`.

### 3.3 CLI (`openride run`, `scenario new/edit`, `run-simulation`, `validate-run-policies`)

- `--policy dispatch=PooledMarket` sets the top type. A type change resets params to the new type's defaults, except that a new decomposition keeps the current per-scope solver (the current `inner`, or the current top when that is a leaf) as its `inner`, with that solver's params, unless the request names the inner's type; every other param resets. (Reworded by the final review fixes, MINOR-8; see Deviations.)
- `--policy-param dispatch.inner=RandomAssignment`: a string assigned to a ref sets its type and resets its params.
- `--policy-param dispatch.inner.candidates=spatial` and `--policy-param dispatch.offer.keep_below_km=3`.
- Parsing: the slot is the longest prefix that matches a known slot name. Every non-final path segment is a ref.
- `--solver X` and `ORSIM_SOLVER` set the **innermost leaf**: descend `inner` refs; if the top has none, replace the top. X must be a leaf (message 11).
- Bench aliases are allowed: `--policies pm=PooledMarket,pr=PooledMarket --policy-param pr.inner=RandomAssignment`.

### 3.4 Dashboard

The "Operator policies" card gets:
- A dispatch dropdown, with an information-model badge on each option.
- A schema-driven `PolicyParamsForm`, where a ref param renders a dropdown of its kind plus a nested `PolicyParamsForm` (recursive, indented card) and a nullable number gets an "unset" checkbox.
- No hardcoded lists.
- The Run step's **structure dropdown is untouched**: it is world data and the one-dataset/many-arms mechanism (doc 21 §2a).

### 3.5 Stamp (`run_config.meta.policies.dispatch`)

Fields:
- `name`, `version`, `kind`, `timing`, `source`.
- `label`, e.g. `"PooledMarket/GreedyNearest"`: the top name plus the `inner` chain.
- `leaf`.
- `information_model`, `share_tag_format`.
- `params`, nested; each ref is `{type, version, params}`.
- `candidate_settings`, `accounting_params`.

The `Policies effective:` console line prints the label, the model and the params.

### 3.6 Legacy translation (deterministic; one pure leaf function `policies/dispatch/legacy.py`, used by preprocess, the profile resolver and the env converter)

| Legacy input | New selection |
|---|---|
| spec `solver` (or `planner.deployment.type`, else the default), params from `deployment.params` ⊕ `solverParams`, `planner.topology` absent or `partitioned` | `{type: Partitioned, params: {inner: {type: S, params: P∩declared}}}` |
| the same with `planner.topology` set to `pooled` or `two-stage` | `{type: PooledMarket, params: {max_rounds, offer, claim, arbitration from planner.market (defaults OfferAll / ClaimAllPlanned / LowestCost / 20), inner: S}}` |
| a bundle profile without `policies`: `strategy`, `solver_params`, `use_spatial_matching`, `planner` | the same as above; `use_spatial_matching: false` becomes `inner.candidates = "sampled"` where the leaf declares it; no `strategy` means `RandomAssignment` |
| undeclared leaf or component params | dropped at WARNING (the one existing legacy-tolerance rule) |
| an unknown topology, an unknown market component name, or `max_rounds` outside 1..100 or not an int | **loud** `PolicyError` / `SpecValidationError`. Today the runtime fails soft (S6, S15) |
| `ORSIM_SOLVER` / `--solver S` | innermost leaf becomes S, with params reset; run-name segment ` · S` (unchanged) |
| `ORSIM_PLANNER_TOPOLOGY=pooled\|two-stage\|partitioned` | top becomes PooledMarket or Partitioned, keeping the current inner. The market params come from the current PooledMarket, else from `profile.planner.market`. Segment ` · pooled` / ` · partitioned`, appended at today's position |
| `ORSIM_OFFER_POLICY`, `ORSIM_CLAIM_POLICY`, `ORSIM_ARBITRATION_RULE`, `ORSIM_MARKET_MAX_ROUNDS` | the PooledMarket component type (params reset) and `max_rounds` (1..100). The top must be PooledMarket after the topology step, else message 9. Today 11..100 were ignored |
| previous-round `policies.dispatch: {type: GreedyNearest}` (unreleased) | literal **central** GreedyNearest; bundles compiled that way are refused at run start through the mirror check (message 13) |
| `solver` / `solverParams` next to `policies` | must equal the innermost leaf's type and params |
| `planner.deployment` next to `policies` | the existing rule, retargeted to the leaf |
| authored `planner.topology` next to `policies` | must equal the echo (Partitioned ↔ partitioned, PooledMarket ↔ pooled); for any other top, every value is an error |
| authored `planner.market` next to `PooledMarket` | must agree with its params |
| authored `planner.market` next to another top | tolerated as inert pooled-arm configuration, read only by `ORSIM_PLANNER_TOPOLOGY` |

**Compile mirrors**, through one helper `mirror_dispatch_selection`:
- `strategy` becomes the leaf.
- `solver_params` and `deployment` are mirrored (existing keys only).
- `planner.topology` becomes the echo; the key is removed when the echo is `None`.
- `planner.market` becomes the legacy shape of the PooledMarket params when PooledMarket is selected.

At run start the compiled `profile.policies` is authoritative and the mirrors are *checked*, not read.

### 3.7 Error messages (exact; `where` paths as shown)

1. `policies.dispatch.params.inner: unknown dispatch policy 'GreedyNeerest'. Registered dispatch policies: GreedyNearest, Partitioned, PooledMarket, RandomAssignment. (List them: openride policy list dispatch)`
2. `policies.dispatch.params.inner: 'PooledMarket' decomposes the tick itself (it has an 'inner' of its own); an inner must be a per-scope solver: GreedyNearest, RandomAssignment.`
3. `policies.dispatch.params.offer: unknown market.offer component 'OfferAl'. Registered: OfferAll, OfferNone, OfferSpare. (List them: openride policy list market.offer)`
4. `policies.dispatch.params.inner must be an object {type, params?} (got 'GreedyNearest'). In spec.json write {"type": "GreedyNearest"}; on the CLI --policy-param dispatch.inner=GreedyNearest is accepted.`
5. `policy overrides.dispatch.params for Partitioned: unknown key 'dual_cycle_bonus_km'. Declared params: inner. 'dual_cycle_bonus_km' is a param of the inner GreedyNearest: use dispatch.inner.dual_cycle_bonus_km.` (This hint is added whenever the inner declares the key.)
6. `assignment profile planner.topology='nonsense' is not a legacy topology (partitioned, pooled, two-stage). The decomposition is now the dispatch policy: policies.dispatch.type = Partitioned or PooledMarket.`
7. `spec sets planner.topology='pooled' but policies.dispatch.type='Partitioned'. planner.topology is the legacy spelling of the dispatch decomposition (pooled = PooledMarket, partitioned = Partitioned); remove planner.topology.`
8. `spec sets planner.market that disagrees with policies.dispatch PooledMarket params (<k>=<a> vs <b>, …). planner.market is the legacy spelling of the PooledMarket params; move the values into policies.dispatch.params and remove planner.market.`
9. `ORSIM_OFFER_POLICY='OfferSpare' configures the PooledMarket dispatch, but this run's dispatch is Partitioned/GreedyNearest. Add ORSIM_PLANNER_TOPOLOGY=pooled, or select --policy dispatch=PooledMarket --policy-param dispatch.offer=OfferSpare.`
10. `ORSIM_PLANNER_TOPOLOGY='pooled' selects PooledMarket but ORSIM_POLICY_OVERRIDES sets dispatch.type='Partitioned'; pass one of them.`
11. `--solver 'PooledMarket' names a decomposition, not a per-scope solver. --solver sets the inner solver (GreedyNearest, RandomAssignment); select a decomposition with --policy dispatch=PooledMarket.`
12. `--solver 'RandomAssignment' conflicts with dispatch.inner.type='GreedyNearest' from --policy-param/--policy-overrides; --solver is shorthand for the innermost solver, pass one of them.`
13. `assignment agent '<id>': profile planner.topology='pooled' disagrees with its compiled policies.dispatch (Partitioned). The compiled policies block is authoritative; change a run's decomposition with --policy dispatch=PooledMarket (ORSIM_PLANNER_TOPOLOGY is translated into exactly that). Fix the bundle or recompile.` The same shape is used for `strategy` versus the leaf and `planner.market` versus PooledMarket.
14. `cooperation structure '<id>': pool '<pid>' lets <a> and <b> share orders but the structure's adjacency does not pair them. Pools and adjacency must agree; recompile the scenario (pools are canonical).`
15. `@policy(slot='dispatch', name='X'): share_tag_format='v1' is reserved for the Partitioned built-in (the pre-v2 wire format, kept only for byte identity); new policies use the default 'audited'.`
16. `@policy(slot='dispatch', name='X'): declare information_model= one of central, per_group, per_company (whose private data this policy's decision may use).`
17. `@policy(slot='dispatch', name='GreedyNearest'): arm_budgets_ms key 'candidate=sampled' is not an arm of this policy. Arms: candidates=sampled.`
18. `DISPATCH POLICY CONTRACT VIOLATION: <label> v<version> at step <n> returned an invalid tick plan: <problems>. The plan was DISCARDED (nothing committed this tick). This is a policy bug: run \`openride policy validate dispatch <name>\`.`
19. `DISPATCH POLICY CONTRACT VIOLATION: inner <name> v<version> in scope <key> at step <n> returned an invalid plan: <problems>. The scope's plan was DISCARDED (nothing committed for this scope this tick). …`
20. `DISPATCH POLICY CRASHED: inner <name> raised for company '<hid>' in round <r> at step <n>; that company commits nothing this round (pooled-market behaviour, unchanged). Traceback follows. (celery journal, not sim.log)`
21. `--policy-param 'dispach.inner=X': no known slot is a prefix of 'dispach.inner'. Known slots: dispatch.` and `--policy-param '<arg>' must be SLOT.PATH=VALUE with a non-empty path, e.g. dispatch.inner.dual_cycle_bonus_km=0.`
22. `dispatch fixture <path> is format openride.dispatch_context/1 (one scope); this version replays whole ticks (format /2). Re-capture with --capture-dispatch.`

---

## 4. Module layout (one responsibility each)

**New**

| File | Responsibility |
|---|---|
| `policies/framework/components.py` (leaf) | Component kinds (`market.offer`, `market.claim`, `market.arbitration`): the live registry (lazy import), default, description; `RefTarget`; `ref_target(kind, name)`; `ref_names(kind)` |
| `policies/dispatch/legacy.py` (leaf, stdlib plus framework errors) | `LEGACY_LEAF_DEFAULT="RandomAssignment"`, `LEGACY_TOPOLOGIES`, `legacy_topology(planner)`, `legacy_market_params(planner)` (strict), `wrap_legacy_leaf(leaf_entry, planner)`, `topology_echo(type)`, `market_echo(params)` |
| `policies/dispatch/world.py` | `DispatchWorld`, `tick_seed(run_id, step)`, `validate_cooperation_world(profile)` |
| `policies/dispatch/compose.py` | Reusable decomposition helpers (§2.3) |
| `policies/dispatch/composite.py` | `CompositePolicy(DispatchPolicy)`: holds `inner` and `inner_node`; forwards `set_rng`, `set_tiebreak` and `set_rebate_book` to the inner |
| `policies/dispatch/library/partitioned.py`, `library/pooled_market.py` | The two decomposition solvers |
| `assignment/accounting.py` | `ShareAccount`, `owner_reserve_km` (moved), `finite_or_none` (moved), `account_shares`, `render_share_tags` |
| `tests/golden/compare.py` | F9 published-payload comparison |

**Changed**

| File | Change |
|---|---|
| `framework/params.py` | `PolicyRef`, `ref()`, nullable numbers, schema |
| `framework/registry.py` | New `@policy` kwargs `information_model`, `share_tag_format`, `module_rng`, `arm_budgets_ms`; `PolicySpec.refs` and `is_composite` |
| `framework/slots.py` | 5 slots; dispatch default `Partitioned`; drop the `legacy` status and `configured_via` |
| `framework/resolve.py` | Nested resolve, merge, CLI parse, stamp, label |
| `framework/catalog.py` | Adds `components`, plus the new policy fields |
| `dispatch/context.py`, `scopes.py`, `runner.py` | Whole tick (`runner.run(ctx) -> TickOutcome(pairs, notes)`; no more partition/company methods) |
| `dispatch/profile.py` | Tree resolution; `build_policy_tree(node, reuse=None)`; `bind_instance`; `mirror_dispatch_selection` (replaces `mirror_dispatch_params`); run-start check including mirrors and world |
| `dispatch/resolved.py` | `ResolvedDispatch` gains `information_model`, `share_tag_format`, `label` |
| `dispatch/base.py` | Adds `decision_rng(ctx)`, a context manager that binds `_rng` and `_tiebreak_seed` to the context for one decision and restores them in `finally` |
| `dispatch/pair_solver.py` | Default `decide` runs under `decision_rng` |
| `dispatch/capture.py` | Format /2, whole tick |
| `assignment/solver/greedy_nearest.py`, `random_assignment.py` | `information_model="central"`, `decision_rng`; Greedy gets `arm_budgets_ms` |
| `assignment/solver/__init__.py` | `DEFAULT_SOLVER = LEGACY_LEAF_DEFAULT` |
| `assignment/policy/offer.py`, `claim.py`, `arbitration.py` | A nested typed `Params` and `version="1"` per class; the getters stay as library functions |
| `assignment/pooled_planner.py` | Engine only (§2.3) |
| `assignment/app.py` | §5 C3 |
| `datagen/preprocess.py`, `scenario/spec_compile.py`, `scenario/frontend_scenario_spec.py`, `scenario/scenario_overrides.py`, `scenario/scenario_manager.py` | Selection plumbing (C4) |
| `apps/simulation/run_container_logistics_simulation.py` | Combined override application, labels, stamp |
| `openride_control/policies.py`, `command.py`, `manager.py` | Validation order, separate `--solver`, `components` |
| `openride/policy_cmd.py`, `run_group.py`, `scenario_cmd.py` | CLI |
| `policies/testing/fixtures.py`, `battery.py`, `bench.py` | Whole tick |
| `tests/golden/capture_policy_golden.py` | Format 2 → `policy_golden_v2.json.gz`; `policy_golden.json.gz` is deleted |
| `docs/policy_authoring.md`, `docs/plans/26-policy-slots.draft.md` | Rewritten for the new model |

**Deleted:** `AssignmentApp._assign_pooled`, `_market_components`, `_resolved_topology`; `DispatchRunner.partition_context`, `company_context`, `company_rule` (moved to `compose.ScopeDecider`); `framework.catalog._legacy_names`.

**FE:** `lib/policy-slots.ts` (+test), `components/scenario/PolicyParamsForm.tsx`, `PolicySlotsEditor.tsx`, `app/api/scenarios/policies/route.ts` (adds `components`), `lib/compareRunMeta.ts` (+test), `lib/runNameMeta.ts` (+test).

**Import-cycle rule, extended.**
- Leaves: `framework/*` (`components.py` imports the registries locally, in a function), `dispatch/{base,plan,pair_solver,resolved,legacy}.py`.
- `app.py` may import `assignment.accounting` at top (it imports only `constraints`). It imports `policies.dispatch.*` only locally: `_build_dispatch_policy`, `_dispatch_runner`, `_dispatch_world`.
- `pooled_planner` may import `policies.dispatch.legacy` (a leaf) and nothing else from `policies`.
- `preprocess` may import exactly `policies.dispatch.legacy` (precedent: it already imports `apps.container_logistics.rebate`).

---

## 5. Ordered work items

### C0: F9 golden comparison (tests only; must pass on `c30fd25` before any production change)

**`tests/golden/compare.py`**
```python
TRANSPORT_TICK_KEYS = ("sim_clock", "time_step")
def published_mismatch(actual, expected, *, sim_clock: str, time_step: int) -> str | None
```
For each `(topic, payload)`:
- Topics must be equal.
- Equal payload strings pass.
- Otherwise `d = json.loads(actual)`. If neither key is in `d`, report a mismatch (the exact comparison failed).
- If exactly one key is present, report a mismatch.
- If both are present, both must equal the harness tick: `d["sim_clock"] == sim_clock` (a str) and `type(d["time_step"]) is int and d["time_step"] == time_step`. Delete both, then require `json.dumps(d, default=str) == expected` exactly. That keeps key order, float repr and escaping of everything else exact.

**Edits:** `test_dispatch_golden_identity.py` and `test_dispatch_golden_via_init.py` compare `published` through it (`sim_clock = tick_states[case.tick_state].sim_clock`, `time_step = TIME_STEP`). All other fields stay exact. The fixture and `harness.py` are untouched.

**Tests:** `tests/test_golden_published_compare.py`:
- `test_identical_strings_pass`
- `test_appended_tick_keys_equal_to_the_harness_tick_pass`, using a real stored payload with `, "sim_clock": "...", "time_step": 42` re-dumped
- `test_wrong_time_step_fails`
- `test_bool_time_step_fails`
- `test_only_one_tick_key_fails`
- `test_any_other_added_key_fails`
- `test_changed_benefit_value_fails`
- `test_reordered_payload_keys_fail`

Each test's docstring says "Verified to have teeth" after a mutation (for example, stripping without checking the values).

**Merge note:** do not edit the two main-tree hunk anchors (S17). In `assign()` the lines `# Reset the collaboration tags…`, `self._share_tags = {}` and `prof = self._assignment_profile()` stay adjacent and unchanged. `publish()` stays byte-unchanged. Imports and every other line are free to change.

### C1: framework types, components, registry, slots, catalog

**`params.py`**
- `@dataclass(frozen=True) class PolicyRef: type: str; params: PolicyParams | None = None; version: str | None = None`.
- `def ref(type_name: str) -> PolicyRef`.
- `param(..., kind: str | None = None, leaf_only: bool = False)`: `kind` is allowed only on fields annotated `PolicyRef`.
- `from_mapping` resolves refs through a hook. `framework/components.py` registers kind resolvers into `params._REF_RESOLVERS` at import, which avoids a params ↔ registry cycle; the `dispatch` resolver uses `registry.get_policy_spec`.
- Default refs are resolved lazily. Registration checks only the ref's shape, because of discovery order. Full default resolution is checked by `test_every_policy_default_tree_resolves` and the battery's `params.defaults_validate`.
- Nullable: `float | None` and `int | None`; bounds apply to numbers only. `to_dict` renders refs nested as `{"type", "params"}`; `to_profile_dict` adds `version`.

**`registry.py`**
- `policy(..., information_model=None, share_tag_format="audited", module_rng=None, arm_budgets_ms=None)`.
- For slot `dispatch`, `information_model` is required (message 16).
- `"v1"` is allowed only for name `Partitioned` (message 15).
- `arm_budgets_ms` keys that name a str-choice arm are validated at registration (message 17). Ref-arm keys are validated by a test.
- New `PolicySpec` fields: `information_model`, `share_tag_format`, `module_rng`, `arm_budgets_ms`, and `refs: tuple[(param, kind, leaf_only)]`. Property `is_composite = any(kind == "dispatch")`.

**`slots.py`:** 5 slots; the dispatch default is `"Partitioned"`.

**`catalog.py`:**
- `policy_catalog() -> {slot: …}` is unchanged in shape.
- Dispatch entries add `information_model`, `composite`, `share_tag_format`, `module_rng`, `order_sensitive`, `arm_budgets_ms`.
- New `component_catalog() -> {kind: {default, description, policies:[{name, version, description, params_schema, defaults}]}}`.
- `openride_control.scenario.cmd_known_policies` returns `{**known_policies(), "slots": …, "components": …}`.

**Market component typed `Params`:**
- `OfferSpare`: `keep_below_km: float | None = param(None, ge=0, help="… None = no threshold (infinity)")`, `only_when_short: bool = param(True, …)`.
- `ClaimIfGainExceeds`: `min_gain_km: float = param(0.0, …)`, no bound.
- All others are empty.
- A component class without a `Params` (a test injection) is treated as declaring no params.

**Built-ins:** Greedy and Random get `information_model="central"`.

**Tests**

`tests/test_policy_framework_params.py`, extended:
- `test_ref_param_accepts_registered_target_and_fills_its_defaults`
- `test_ref_param_rejects_unknown_target_listing_registered` (message 1)
- `test_ref_param_rejects_composite_where_leaf_only` (2)
- `test_ref_value_must_be_object_with_type_and_params_only` (4)
- `test_nested_param_error_names_full_path`
- `test_nullable_number_accepts_none_and_bounds_apply_to_numbers`
- `test_json_schema_for_ref_and_nullable_uses_only_supported_keywords`
- `test_to_dict_roundtrips_nested_refs`

`tests/test_policy_framework_registry.py`, extended:
- `test_dispatch_policy_must_declare_information_model`
- `test_share_tag_format_v1_reserved_for_partitioned`
- `test_arm_budget_keys_must_name_arms`
- `test_composite_flag_from_a_dispatch_ref`

`tests/test_policy_components.py`, new:
- `test_component_ref_resolves_a_name_injected_into_the_live_registry`
- `test_every_builtin_component_declares_params_and_defaults_validate`
- `test_typed_defaults_reproduce_legacy_empty_params` (each offer/claim/arbitration class, typed-default instance versus a `{}` instance, on a fixed market fixture: identical output)
- `test_offer_spare_keep_below_none_means_infinity`

`tests/test_policy_catalog.py`, this-branch edits: five slots; `components` has 3 kinds; dispatch entries carry the new fields; `known-policies` keeps the datagen keys.

### C2: legacy translation, world, accounting, resolution tree

**`legacy.py`:** pure functions per §3.6.

**`profile.py`:**
- `resolve_profile_policies(prof) -> ({slot: ResolvedPolicy}, source)`, with nested params.
- `resolve_dispatch_for_profile(prof) -> ResolvedDispatch`, a typed tree.
- `build_policy_tree(node, reuse: DispatchPolicy | None = None) -> DispatchPolicy`.
- `bind_instance(node, instance) -> DispatchPolicy`:
  - a registered class whose name equals the top: reuse it (its inner names must match recursively, else raise);
  - else, descend `inner`;
  - a registered class whose name equals the innermost leaf: build the wrappers around the instance;
  - an unregistered class: it becomes the innermost leaf under the legacy candidate rule;
  - otherwise raise `PolicyError("AssignmentApp._solver is a X but the profile resolves dispatch to Partitioned/Y")`.
- `mirror_dispatch_selection(profile, resolved)` and `check_mirrors(profile, resolved)` (message 13).
- `assert_assignment_profiles_resolvable(collection)` now also runs `check_mirrors` and `validate_cooperation_world`.

**`world.py`:** see §2.2.

**`accounting.py`:** see §2.5.

**Tests**

`tests/test_policy_legacy_translation.py`, table-driven over §3.6:
- `test_legacy_spec_translates[row]`
- `test_legacy_profile_translates[row]`
- `test_unknown_topology_is_loud`
- `test_two_stage_is_pooled_market`
- `test_unknown_market_component_is_loud`
- `test_undeclared_component_params_dropped_with_warning`
- `test_translation_is_deterministic` (same input → same JSON bytes)
- `test_legacy_market_params_agree_with_resolve_market_config_on_valid_inputs` (a grid)

`tests/test_share_accounting.py`:
- `test_v1_render_reproduces_every_stored_partitioned_golden_share_tag`: for every partitioned golden case, `render(account(expected pairs, tick-state candidates))` equals the stored `share_tags`. The candidates are rebuilt by applying `filter_assignable_trucks` to the stored tick state.
- `test_audited_render_reproduces_stored_pooled_tags_except_market_notes`: pool_id and market_round are fed from the stored tags.
- `test_v1_non_finite_awarded_cost_renders_minus_inf`
- `test_audited_non_finite_renders_none`
- `test_reserve_is_min_over_owner_free_feasible_trucks`
- `test_only_whitelisted_pair_notes_reach_the_tag`

`tests/test_dispatch_world.py`:
- `test_world_rule_equals_partition_rule_on_a_pair_grid` (a frozen oracle copy of `PartitionRule.allowed`)
- `test_cooperation_world_check_refuses_pools_outside_adjacency` (14)
- `test_normalized_structures_pass_the_check[edges|pools]`
- `test_cooperation_pairs_include_self_loops`

`tests/test_dispatch_profile_resolution.py`, this-branch edits: `test_bind_leaf_instance_at_innermost`, `test_bind_composite_instance_at_top`, `test_bind_mismatch_names_the_tree`, `test_unregistered_leaf_uses_legacy_kind_rule`, `test_mirrors_written_and_checked`, and the cache-rebuild test extended so that a `planner` patch rebuilds the runner.

### C3: whole-tick runtime (HARD GATE: golden)

**`app.py`**
- `__init__`: `self._solver = build_policy_tree(resolve_dispatch_for_profile(prof))`, wrapped with the stale-worker hint as today.
- `_dispatch_runner(prof)`: the cache key adds `json.dumps(prof.get("planner"), sort_keys=True, default=str)`; it calls `bind_instance`.
- `_dispatch_world(prof)`.
- `assign()`: the body after `candidates` is replaced by the §2 flow:
  - `tick_seed = self._tick_seed(str(getattr(self, "run_id", "")), int(time_step or 0))`;
  - `execute` is `_eventlet_tpool.execute` when `solve_in_thread` is set, else a direct call;
  - log `Pooled cooperation:` when there are market notes, else `Cooperation:`;
  - the haulier-block warning is unchanged.
- `_record_round_stats(market_notes, time_step)` reads `notes["max_rounds"]`; its body is otherwise unchanged.
- `_tick_seed` delegates to `world.tick_seed`.
- `round_stats` and `_patch_round_stats_to_run_config` are unchanged.

**Byte-identity argument, partitioned default (legacy profile, `__new__` or `__init__`)**
1. The inputs and `candidates` lines are unchanged.
2. `world.rule` is `PartitionRule(mtt, share_adjacency(active_cooperation_structure(prof)))`, the same rule as S2.
3. Building the tick context draws no RNG. Its `rng` is lazy and Partitioned never reads it, so no `Random` is created.
4. `cooperation_groups` is a verbatim move of S2: the same keys, insertion order, skips and buckets.
5. Each group context equals the old `partition_context`: `rng = inner.rng`, which is the module because nothing injects; `tiebreak = None`; `sample_rng = random`; the same spatial source.
6. `decision_rng` binds `_rng` to the module object and `_tiebreak_seed` to `None` for the call, then restores them, so the draws are identical. `check_plan` on each sub-scope does what the old runner did.
7. The group plans merge in group order, so the commit order equals the old `assignment.extend` order.
8. The tick-level `check_plan` and the heads mapping are pure, so there are no RNG draws. The pairs are the same document objects, with first doc per id over `candidates`; there are no duplicates in valid input.
9. Accounting runs after all solves instead of interleaved with them. It is pure, and its truck set equals S3's: the owner's trucks all sit in the owner's bucket, and the used set restricted to those trucks is the same. `v1` renders the exact S3 arithmetic.
10. `publish` is unchanged, so `published` is identical.
11. Module RNG consumption is unchanged, so `rng_probe` is identical. With `solve_in_thread` set, `execute` is still called once per group, as today.

**Byte-identity argument, pooled default**
1. `tick_seed` uses the same blake2b formula (the harness sets `run_id`). `ctx.rng` is `Random(tick_seed)`, created the first time PooledMarket reads it.
2. `set_rng` and `set_tiebreak` are called on the same leaf instance, before any draw, in the same order as S4.
3. The components are equivalent (C1 test), and `trucks_by_haulier`/`orders_by_haulier` and the `PoolMarket` are built by moved code.
4. The engine body is unchanged up to step 6. Removing the tag computation removes no draws.
5. `ScopeDecider` reproduces the old runner: the same `candidate_kind`, company context (`sample_rng = ctx.rng`) and `run`.
6. The audited render equals S7b because `awarded == float(accounting_cost) == award.cost_km` (S8), and the reserve uses the same function, trucks and committed set.
7. The round stats get the same inputs, and `max_rounds` is the validated value, equal to the old clamp on valid configs.
8. No module draws.

**Edited pre-existing tests (intended contract changes; nothing else may change)**

| Test | New form |
|---|---|
| `test_assignment_pooled.py::test_unknown_policy_names_are_fail_soft_and_still_assign` | `test_unknown_market_component_names_fail_loud`: `assign` raises `UnknownPolicyError` naming `NopePolicy` and listing the registered names |
| `::test_two_stage_topology_is_an_alias_for_pooled` and `::test_unknown_or_missing_topology_falls_back_to_partitioned` | Retargeted to `legacy.legacy_topology`: `two-stage` → pooled; `{}` and `{planner: {}}` → partitioned; `nonsense` raises (6) |
| `test_pool_housekeeping.py::test_market_components_pick_up_a_behavior_patched_after_construction` | `_dispatch_runner(prof).resolved.params.max_rounds` goes 3 → 9 after the patch |
| `test_assignment_pooled_convergence.py::test_stamp_market_matches_what_the_app_actually_instantiates` | `effective_cooperation_stamp(profile)["market"] == market_echo(resolve_dispatch_for_profile(profile).params)`, and the PooledMarket component types instantiated from that profile match |
| The via-`__init__` golden | Asserts the innermost leaf of `app._solver` (`innermost_policy` in `composite.py`) |
| Docstring-only change: `test_assignment_pooled_benefit.py:51` | The location of `owner_reserve_km` |

**New tests: `tests/test_whole_tick_dispatch.py`**
- `test_partitioned_groups_match_the_frozen_inline_bucketing` (oracle: a copy of `app.py` 615-700 at `c30fd25`; golden tick A, chain and no-coop)
- `test_partitioned_never_constructs_the_tick_rng` (monkeypatch `random.Random` to count constructions inside `assign`; expect 0 partitioned, 1 pooled)
- `test_disallowed_cross_company_head_is_discarded_by_the_framework`: an `isolated_registry` central policy pairs a cargo truck with an acme order on chain A-B-C. Expect `assign() == []`, the ERROR message 18, `_share_tags == {}`, `stats["violations"] == 1`.
- `test_scope_violation_discards_only_that_group`
- `test_pooled_market_selection_equals_legacy_topology_path` (pairs, tags, published strings and round stats are equal)
- `test_central_greedy_serves_only_world_allowed_cross_pairs_and_renders_audited_tags`
- `test_central_leaves_draw_no_module_rng[GreedyNearest-spatial|GreedyNearest-sampled|RandomAssignment]`
- `test_market_crash_logs_crashed_prefix_counts_and_skips_company`
- `test_rebate_book_reaches_the_leaf_through_a_composite`

Update `tests/test_policy_import_cycles.py` to include `assignment.accounting` and the new modules (they are picked up automatically by its walk).

### C4: selection plumbing

**`preprocess._normalize_policies`** wraps through `legacy.wrap_legacy_leaf`. With an authored block it runs the conflict checks of §3.6 (messages 7 and 8, the `solver` and `solverParams` checks against the leaf). Note: `_normalize_planner` always fills a default topology, so the checks read the raw spec's `planner`.

**Recipe echo:** an authored planner is echoed verbatim.

**`spec_compile`:** resolves the tree, calls `mirror_dispatch_selection`, retargets `_refuse_deployment_contradicting_policies` to the leaf, and `_patch_assignment_profiles` asserts `strategy == leaf`.

**`frontend_scenario_spec`:**
- `drop_dispatch_dials_superseded_by` also drops the carried `planner.topology` and `planner.market` next to a `policies` body.
- `known_solvers()` returns `{"solvers": leaf names, "default": LEGACY_LEAF_DEFAULT}`.
- `normalize_solver` returns a leaf.
- editForm `policySlots` is nested.

**`scenario_overrides`:**
- `run_dispatch_overrides(collection, *, overrides, solver, topology, offer, claim, arbitration, max_rounds) -> (merged_overrides, labels: {"dispatch": str|None, "topology": str|None})` is the one function the sim main and the control both call. Order: explicit overrides; topology; market env; `--solver` (leaf). Conflicts are messages 10 and 12.
- `apply_planner_topology_override` and `apply_market_override` become wrappers: the same returns, now loud.
- `apply_policy_overrides` re-mirrors.

**Sim main:**
- One application at the old policy-override spot.
- The dispatch label is appended there and the topology label at its old spot.
- The run-start check, then the stamp (with the new fields).

**Control:**
- `build_run_policy_overrides` no longer folds `--solver` in; it travels separately as `ORSIM_SOLVER`.
- `validate_run_policy_args` first calls `precheck_override_names(overrides, solver)`: every named type and component is registered, and the params of every *named* type are validated against that type. Only then does it load the bundle and call `run_dispatch_overrides`.

**CLI:** nested `--policy-param`. `policy list [slot|kind]` includes components and an info-model column. `policy show` renders refs. `openride run --solver` help reads "innermost per-scope solver".

**Tests**

`tests/test_policies_spec_block.py`, this-branch edits:
- `test_legacy_solver_translates_to_partitioned_inner`
- `test_planner_topology_pooled_translates_to_pooled_market_with_market_block`
- `test_authored_topology_disagreeing_with_policies_is_rejected` (7)
- `test_authored_market_disagreeing_with_pooled_market_is_rejected` (8)
- `test_every_shipped_spec_translates_to_partitioned_with_its_solver`
- `test_policies_block_never_perturbs_generated_agents` (I1, nested)

`tests/test_policies_spec_carry.py`: `test_body_with_policies_drops_carried_topology_and_market`.

`tests/test_legacy_run_env_overrides.py`:
- `test_topology_env_pooled_keeps_inner_and_market_block`
- `test_topology_env_equals_compiled_pooled_scenario_outputs`
- `test_market_env_without_pooled_market_is_loud` (9)
- `test_env_conflicts_are_loud` (10, 12)
- `test_run_name_segments_unchanged_for_legacy_inputs[solver|topology|solver+structure+topology]` (expected names hardcoded from `ec085f5` behaviour)
- `test_market_max_rounds_env_range_1_to_100`

`tests/test_policy_run_overrides.py`: nested-path edits; `test_solver_is_innermost_leaf`; `test_solver_naming_a_composite_is_rejected` (11).

`tests/test_policy_override_equals_compiled_edit.py`: the path becomes `dispatch.inner.dual_cycle_bonus_km`; add a PooledMarket case.

`tests/test_validate_run_policies_order.py`: `test_bad_solver_reported_even_without_a_bundle`, `test_bad_nested_ref_reported_before_bundle_lookup`.

`tests/test_scenario_market_overrides.py`, edits:
- `fail_soft` → `fail_loud` (unknown topology raises)
- `patches_only_loaded_behaviors`: a type change resets params; the mirror has the legacy shape
- `rejects_out_of_range`: 0, 101 and `"x"` raise; 11 is applied
- `accepts_unknown_algorithm_names` → `rejects_unknown_component_names`

### C5: battery, bench, capture, policy golden

**Fixtures** are whole-tick: `Fixture(id, seed, trucks, orders, structure, max_travel_time_pickup, settings, time_step, run_id, accounting_params={})`. `Fixture.context(runner, …)` goes through `runner.tick_context`. Add `three_company_chain` (A-B-C plus a solo D) and `missing_haulier`.

**Arms:**
- The default.
- Each non-default choice of each own `str` param.
- For each ref param, every other available target of its kind that satisfies `leaf_only`, at its defaults.
- Not crossed and not recursive.

**Per-arm budgets (the rule):** every arm is gated by `arm_budgets_ms.get(arm, perf_budget_ms)` on `scale`, best of 3.
- A budget is a *measured property of an arm*: `max(3 × worst of 2×5 runs, 500)`, with the numbers written at the decorator.
- An arm that has not been measured inherits the default arm's budget, so it fails loudly if it is slower, which forces a measurement.
- Measure and declare `GreedyNearest` `candidates=sampled` (about 1.6 s measured, so about 5000 ms), plus every Partitioned and PooledMarket arm.
- Note in the docstring: a budget above `STEP_SETTLE_SECONDS` (2000 ms) means that arm makes the assignment agent a per-step straggler at 1000×500.

**Checks:**
- Add `information.model_declared`, `information.scopes_respect_model` and `params.rejects_bad_ref`.
- `no_module_rng` and `order_independent` are skipped (ok, with the reason) when any node of the arm's selection declares `module_rng` or `order_sensitive`; these compose.
- The battery no longer injects `set_rng`/`set_tiebreak`. The context is the only randomness input, and the module RNG is seeded per decide.

**Tests:**
- `tests/test_policy_contract_battery.py` runs over the 4 built-ins, all arms, in under 120 s.
- `tests/test_policy_battery_catches_broken_policies.py`, new cases: `LeakyMarket` fails only `information.scopes_respect_model`; `CrossCompanyCentral` fails only `structure.only_allowed_pairs`; `SlowUndeclaredArm` fails only `performance.scale[candidates=x]`.
- `test_arm_budget_keys_name_real_arms`.

**Capture /2:** one file per captured tick, `step_<n>.json.gz`, containing format, run_id, time_step, sim_clock, horizon, `tick_seed`, `module_rng_state`, selection (stamp form), candidate_settings, world `{structure, max_travel_time_pickup, accounting_params}`, trucks, orders, `live_heads`, and `rebate_aware`. Replay refuses `rebate_aware: true` and format /1 (message 22).

**Bench:** whole tick. Aliases. Adds the columns `info_model`, `cross_company`, `benefit_km_total` (from `account_shares`, the same function for every policy), plus the existing ones.

**`policy_golden_v2.json.gz`:** Greedy and Random stay at version "1"; dispatch golden v1 proves their behaviour is unchanged. Add Partitioned "1" and PooledMarket "1". State in the commit message that only the harness format changed.

**Tests:** `tests/test_dispatch_capture_and_bench.py` (this-branch edits): `test_replay_whole_tick_reproduces_live_heads[partitioned|pooled]`, `test_format_1_is_refused`, `test_bench_reports_framework_accounting_columns`; `tests/test_policy_golden_versions.py` (v2).

### C6: FE (`feat/policy-slots`, never in the served checkout)

**`lib/policy-slots.ts`:**
- `ParamSchema.type` may be a `[T, 'null']` tuple; add `x-policy-ref` and `x-leaf-only`.
- `parseCatalog(slots, components)`.
- `fieldKind` adds `'policy'` and `'nullable-number'`.
- Recursive `validateParamValues(schema, values, catalog, path)`.
- Path-based `withParam(sel, slot, path[], value)` and `withRefType(sel, slot, path[], name, catalog)` (resets the ref's params to its defaults).
- `defaultSelection` builds nested defaults.
- `informationModelLabel`.

`PolicyParamsForm` renders refs recursively. `PolicySlotsEditor` shows the badges and drops the legacy-slot summary. The policies route returns `components`. `compareRunMeta.runTagsOf` reads the solver tag from `meta.policies.dispatch.label` and adds the stamped top and leaf names to the parser's known list.

**Tests:** `policy-slots.test.ts` covers refs (unknown target, leaf-only, nested errors with path), nullable, `withRefType` reset, and `selectionForApi` nested shape. `compareRunMeta.test.ts` covers the label. `runNameMeta.test.ts` checks that a `PooledMarket` segment with a stamped name is not read as a structure.

### C7: docs (in-repo)

**`docs/policy_authoring.md`**, rewritten:
- Whole-tick context and the `world`.
- Leaves versus composites, and refs (`ref()`, `leaf_only`).
- Information model.
- Using `compose` helpers.
- Plan notes (reserved keys `market`, `pair_notes`).
- Accounting is not your objective; the two tag formats.
- Determinism (`ctx.rng`, `decision_rng`, `module_rng`, composition of `order_sensitive`).
- Per-arm budgets.
- Capture /2.
- Selection (spec, CLI paths, `--solver` semantics).
- A summary of the legacy table.

**`docs/plans/26-policy-slots.draft.md`:** a new section "Round 2: whole-tick dispatch", the changed slot list, comparability, traps.

Append a "Round 2" deviations section to `docs/plans/policy_slots_p0_p3.md` for the coder.

---

## 6. Restarts and comparability

**Restarts**
- **Celery restart required:** `assignment/app.py`, `accounting.py`, `pooled_planner.py`, `policy/*`, `arbitration.py`, `solver/*`, `policies/**`.
- **None:** sim main, control plane, CLI (subprocesses).
- **No api rebuild:** `run_config.meta` is unschema'd.
- **Dashboard:** HMR plus a hard refresh. Deploy the backend first; the FE requires the `components` key and fails closed without it.
- **Recompile optional:** old bundles translate. A recompile writes the nested `policies` block, and generated agents stay byte-identical (I1). Bundles compiled on this branch in round 1 (flat `policies.dispatch`) are refused at run start (message 13) until recompiled. Recompile them, never hand-patch.

**Comparability**
- Every existing scenario at its defaults is byte-identical: golden 70/70, plus the via-`__init__` subset and the out-of-grid real-bundle check (§7).
- A pooled run selected through `--policy dispatch=PooledMarket` or `ORSIM_PLANNER_TOPOLOGY=pooled` equals the old pooled run (test).

**New arms, not comparable to existing runs:**
- Top-level `GreedyNearest` or `RandomAssignment` (central).
- `RandomAssignment candidates=spatial` (round 1).
- Any `OfferSpare`/`ClaimIfGainExceeds` parameterisation, as before.

All of these are stamped with `label` and `information_model`.

**Fail-loud flips that can refuse launches that used to run:**
- Unknown market component names.
- Unknown topology.
- Mirror disagreement.
- Pool/adjacency inconsistency.
- `ORSIM_MARKET_MAX_ROUNDS` from 11 to 100 is now honoured instead of ignored.

---

## 7. Gates

1. `venv/bin/python -m pytest -q -p no:cacheprovider tests/test_dispatch_golden_identity.py tests/test_dispatch_golden_via_init.py tests/test_golden_published_compare.py`: all green (70 + 15 + new). `git log --follow --oneline tests/golden/dispatch_golden_v1.json.gz` shows only `277e67b`, and `git diff 277e67b HEAD -- tests/golden/dispatch_golden_v1.json.gz tests/golden/harness.py` is empty.
2. Assignment and policy subset: `pytest -q tests/test_assignment_*.py tests/test_container_logistics_*.py tests/test_scenario_market_overrides.py tests/test_pool_*.py tests/test_policy_*.py tests/test_dispatch_*.py tests/test_policies_*.py tests/test_whole_tick_dispatch.py tests/test_share_accounting.py tests/test_legacy_run_env_overrides.py tests/test_random_spatial_candidates.py`: green, apart from the two baseline failures (`test_rebate_aware_flag_defaults_false…`, `test_rules_baseline_does_not_recompile`).
3. Full suite: `venv/bin/python -m pytest -q -p no:cacheprovider --continue-on-collection-errors -rfE 2>&1 | grep -E '^(FAILED|ERROR)' | sort | diff - /tmp/claude-1000/-home-user/f13570e2-81af-483b-8703-76ae77d0783f/scratchpad/baseline/baseline_failures.txt` gives an empty diff. Current is 37 failed / 2188 passed / 5 skipped / 8 errors. Passed goes up by the new tests only; report the exact number.
4. All 16 specs compile into a tmp root (the round-1 one-liner). Each bundle's `policies.dispatch` is `Partitioned{inner: <its solver>}`. Compile → recipe → recompile → recompile gives identical agents.
5. Out-of-grid equivalence. Re-run the round-1 reviewer's comparison: 54 cases through the real `__init__`, `277e67b` (scratch worktree) versus HEAD, partitioned and pooled, over the main-tree bundles `consortium_collab_7d`, `smoke_container_logistics_1d` and `collab_scale_500t_v2`. All identical. Add the pooled-through-env-converter case.
6. Performance. Run a timing script over `tests.golden.harness.run_case`, cases `A-greedy-partitioned-chain-spatial-cap500`, `A-greedy-pooled-chain-spatial-cap500`, `C-greedy-partitioned-nocoop-only-sampled-cap40`, 20 repeats, interleaved, at `277e67b` versus HEAD. The HEAD median must be ≤ 1.05 × old + 2 ms per case. Report both medians and the load average.
7. CLI:
   - `venv/bin/python -m openride.cli policy list`, which shows 5 slots, 3 component kinds and the info models;
   - `policy show dispatch PooledMarket`;
   - `policy validate dispatch {Partitioned,PooledMarket,GreedyNearest,RandomAssignment}`: all PASS;
   - `policy bench dispatch --policies Partitioned,PooledMarket,GreedyNearest --synthetic scale,three_company_chain`;
   - `solver list`, which lists leaves.
8. `python -m openride_control.command known-policies`: `slots` (5) and `components` (3). `validate-run-policies --scenario nope --solver Nope` names `Nope`.
9. `tests/test_policy_import_cycles.py`: green.
10. FE (`/verify-frontend`): `tsc --noEmit` 0; vitest 45 files, 519 plus the new tests.
11. **Live, after the user approves a merge and a celery restart:**
    - `multi_haulier_greedy_solver_500_trucks` n=3 interleaved, and `collab_scale_500t_v2 --structure chain` with `--policy dispatch=PooledMarket` n=3: `empty_ratio`/`dual_cycle_rate` in band, 100% osrm, 0 `DISPATCH POLICY` lines, a stamp label and info model, `meta.market` on pooled only, `benefit == reserve − awarded` on every pooled tag, and the run name unchanged on the default path.
    - One `ORSIM_PLANNER_TOPOLOGY=pooled` run, whose stamp equals the `--policy` arm.
    - A browser edit of a nested ref param.
    - A `--capture-dispatch` run, then bench over it.

---

## 8. Risks, and where the reviewer should look hardest

1. **Accounting re-derivation.**
   - The partitioned (`v1`) `-inf` versus audited `None` handling.
   - `awarded_cost_km` recomputed from the accounting cost instead of read from the bid. This holds only while every claim policy bids `float(cost)`; a future claim with another bid price would now be accounted at the framework cost. That is intended; check the docs say so.
   - The owner-truck set and committed set equivalence (C2 golden-tag replay test).
2. **RNG.**
   - `ctx.rng` laziness: `Partitioned` must never touch it.
   - `decision_rng` restores in `finally`, including on exceptions.
   - The market must call `set_rng`/`set_tiebreak` through the methods: the monkeypatch test.
   - A battery that stops injecting must still be truthful about `module_rng` on Partitioned.
3. **`bind_instance` and the cache key.** A mismatch must raise, never silently rebuild around the wrong leaf; the key must include `planner`.
4. **Duplicate ids.** The tick-level first-doc rule versus the market's old last-doc `trucks_by_id`. Only corrupt input reaches this; confirm the golden has no duplicates.
5. **Legacy env converter.** Conflict detection; the order of run-name segments; `--solver` meaning "innermost"; precheck before the bundle (S19).
6. **Mirrors.** The `strategy`, `planner.topology` and `planner.market` checks at run start must not refuse any main-tree bundle (gate 5 covers it).
7. **Edited pre-existing tests.** Exactly the §5-C3 and C4 list; the reviewer should reject any other edit.
8. **The information model is structural.** Check the `LeakyMarket` teeth, and that `scope_log` costs nothing when off.
9. **`share_tag_format="v1"`** is reserved to Partitioned by registration. Confirm it cannot leak to a new policy.
10. **The ref resolver hook.** A cycle at registration; component registries mutated by tests; the composite inner must be `leaf_only`.
11. **The CLI dotted-slot parse** (future `world.shedding`) and FE/py parity of the nullable and ref schema keywords.
12. **Battery runtime and perf flakiness.** Per-arm budgets on a box that varies 1.3-2×.
13. **Merge zones (S17).** `publish()` byte-unchanged; the top of `assign()` unchanged.

---

## 9. NetworkFlow (P4) and P5

**P4.** Add `policies/dispatch/library/network_flow.py`: `@policy(slot="dispatch", name="NetworkFlow", information_model="central", requires=("ortools",), perf_budget_ms=…, arm_budgets_ms=…)`. Its `decide(ctx)`:
- uses `ctx.trucks`/`ctx.orders` over the whole tick;
- uses `ctx.world.cooperation_pairs(hauliers)` for its `(owner, server)` set and `ctx.allowed` for truck-level feasibility;
- builds the fleet per company from `ctx.trucks`;
- returns `Plan(chains)`.

**What does not change for P4:** the framework still checks every head against the world rule, computes the accounting (audited format), and stamps `central`. `Partitioned(inner=NetworkFlow)` gives a per-group MIP and `PooledMarket(inner=NetworkFlow)` a per-company MIP, both with no new code, because NetworkFlow is a leaf. The contract, selection, runner, invariants, accounting, stamp, catalog, form and battery need no change.

**What P4 does add, all additive:**
- read-only data accessors on the context: `ctx.travel` (an OSRM facility table), facility ids, order time windows;
- making `timing: "plan"` executable: held tails in the runner (the slot's `executable_timings`);
- an optional `fallback: PolicyRef = param(ref("GreedyNearest"), kind="dispatch", leaf_only=True)` time-budget fallback that NetworkFlow handles itself.

**P5:**
- `idle`, `queue`, `world.shedding` and `world.duration` reuse the registry, refs, catalog, form and stamp.
- World slots are comparability boundaries. `NON_WORLD_KEYS` must stop stripping them.
- The cooperation **structure** stays world data, not a slot.
- `market.offer`/`market.claim` were finished here, as components.
- Per-company configuration (for example `PooledMarket.inner_by_company`) needs one more param type: a map of refs.

---

## 10. For the coordinator

**Out-of-repo docs**
- **doc 26 (`/home/user/docs/reference/features/26-policy-slots.md`):** promote the in-repo draft, with:
  - §1 Round 1 / Round 2 tables;
  - §2 "the boundary": world = ownership, cooperation, feasibility, accounting, invariants, commit; solver = decomposition and coordination;
  - the built-ins and their info models;
  - the selection grammar with refs;
  - the legacy table (§3.6);
  - the comparability statement (§6);
  - traps: `v1` tags reserved; `ctx.rng` versus module; mirrors checked at run start; fail-loud flips; restart rule; the deploy order (backend first);
  - verification evidence and the pending live gates.
- **CLAUDE.md §5 pointer:** `- **Policy slots — dispatch is ONE whole-tick solver; partitioned/pooled-market decomposition is a solver choice (Partitioned / PooledMarket), cooperation structure stays world data; accounting is framework-owned** (2026-10) → docs/reference/features/26-policy-slots.md`
- **doc 11 update:**
  - Per-run `--solver X` / `ORSIM_SOLVER` now sets the innermost per-scope solver of the selected dispatch and fails loud on an unknown name (exit 2 / a 400 from the dashboard).
  - `--policy dispatch=…` and `--policy-param dispatch.inner.k=v` select the decomposition and nested params.
  - `ORSIM_PLANNER_TOPOLOGY` and the `ORSIM_OFFER`/`ORSIM_CLAIM`/`ORSIM_ARBITRATION`/`ORSIM_MARKET_MAX_ROUNDS` variables are translated into those and are loud.
  - The stamp is `run_config.meta.policies.dispatch` (label, information_model).
- **doc 21 banner** (recommended): §2's table, §7's fail-soft rule and §11's per-run selection are superseded by doc 26. Topology is now a solver choice; market components are fail-loud.
- **`/restart-for` `SKILL.md` entry:** `apps/container_logistics/policies/**`, `assignment/{app,accounting,pooled_planner,arbitration}.py`, `assignment/policy/*`, `assignment/solver/*` → `systemctl --user restart openride-celery`. `openride_control/policies.py`, `openride/policy_cmd.py`, `apps/simulation/run_container_logistics_simulation.py` → nothing (subprocess).

**Explainer page claims that change**
- **Slots list:** seven slots become five. `market.offer`/`market.claim` are no longer slots; they are PooledMarket components, together with `market.arbitration`.
- **Scope:** "the framework calls a policy once per partition or per company" becomes "the policy gets the whole tick; decomposition is a solver (Partitioned, PooledMarket) built from reusable helpers".
- **Market:** "framework-hosted pooled topology with offer/claim/arbitration registries (fail-soft)" becomes "PooledMarket solver, per_company, components fail-loud; the framework only re-checks feasibility and owns the accounting".
- **Limits and notes:**
  - the information model is declared and stamped, with a structural audit;
  - share tags have two formats with equal values;
  - per-arm performance budgets;
  - P4 no longer says "the scope stays a framework decision";
  - the run-name and stamp label now shows the decomposition, e.g. `PooledMarket/GreedyNearest`.

### Critical Files for Implementation
- /home/user/openride_apps_policy_slots/apps/container_logistics/assignment/app.py
- /home/user/openride_apps_policy_slots/apps/container_logistics/assignment/pooled_planner.py
- /home/user/openride_apps_policy_slots/apps/container_logistics/policies/dispatch/profile.py (plus new compose.py, world.py, legacy.py, library/partitioned.py, library/pooled_market.py)
- /home/user/openride_apps_policy_slots/apps/container_logistics/policies/framework/params.py (plus registry.py, resolve.py, new components.py)
- /home/user/openride_apps_policy_slots/apps/container_logistics/scenario/scenario_overrides.py (plus datagen/preprocess.py, apps/simulation/run_container_logistics_simulation.py, openride_control/policies.py)
---

## Deviations (coder)

Each item says what differs from the text above and why. Anything not listed was
implemented as written.

### C1

1. **The slot default `Partitioned` is declared but not registered until C3.** `slots.py`
   says `default="Partitioned"` as specified; `registry.default_policy_name("dispatch")`
   keeps its strict contract (the default must be registered), so it raises
   `UnknownPolicyError` until C3 registers `Partitioned`. Every runtime reader that
   really meant "the default LEAF" now reads `policies/dispatch/legacy.LEGACY_LEAF_DEFAULT`
   (the final §4/C4 semantics, not a stopgap): `solver.DEFAULT_SOLVER`,
   `dispatch/profile.legacy_dispatch_from_profile`, and in `frontend_scenario_spec`
   `normalize_solver`, the editForm `solver`, the editForm fallback and
   `known_solvers()["default"]`. The one interim piece is the round-1 flat resolver
   `framework/resolve.py`: a block that does not name `dispatch` (and
   `merge_policy_overrides` with no current entry) resolves to `LEGACY_LEAF_DEFAULT`
   through `_unnamed_type` — behaviourally identical to `Partitioned{inner=RandomAssignment}`
   on today's partitioned runtime. C2's nested resolver replaces `_unnamed_type` with the
   slot default. Until C3, `openride policy list` marks no dispatch row as default.
2. `framework/resolve.py` imports the leaf `policies.dispatch.legacy` at module top (for
   item 1). `legacy.py` holds only `LEGACY_LEAF_DEFAULT`; C2 completes it.
3. `PolicyParams.from_mapping` gained `path=` (the dotted location used to name nested
   refs, e.g. `policies.dispatch.params.inner`); it defaults to `where` up to ` for `, so
   every existing caller keeps its messages. Nested params errors read
   `<path>.params for <Target>: ...`.
4. Registration validates defaults with the new `params.check_defaults(cls, where=)`
   (non-ref defaults fully, ref defaults by shape: a known `kind` and a `ref("<name>")`
   default) instead of `from_mapping(None)`, which would resolve refs during discovery.
   `kind` must be a kind with a registered resolver at declaration time; `kind=`/
   `leaf_only=` on a non-ref field and a ref without `kind` are `PolicyRegistrationError`.
5. A ref VALUE may carry `version` (the stamp/compiled echo; ignored on read) besides
   `type`/`params` (`params.REF_ENTRY_KEYS`). Any other key is refused with
   `<path>: unknown key(s) '<k>' in a policy reference. Allowed keys: type, params (and
   version, which is ignored).` (the spec gives no text for this case).
6. `ParamInfo` gained `nullable` and the property `is_ref`; `_ParamMeta` gained `kind`
   and `leaf_only`. A nullable type error reads `must be a number or null`. `str | None`
   is an unsupported type (only nullable numbers were specified).
7. `framework/components.py` also exports `ComponentKind`/`COMPONENT_KINDS`,
   `live_registry(kind)`, `NoParams` (the params of a component class with no `Params`)
   and `instantiate_component(kind, ref)`: builds the class with `params=` the typed
   values minus `None` (so `OfferSpare.keep_below_km=None` takes the class's own `inf`).
   C3's `PooledMarket` should instantiate through it. The legacy getters
   (`get_offer_policy`, `get_claim_policy`, `get_arbitration_rule`) are unchanged in C1
   (still fail-soft, still raw dicts): today's pooled path (`app._market_components`)
   keeps working, and C3 replaces that path.
8. Message 2 names the composite's dispatch ref params (`it has an 'inner' of its own`)
   and the leaves, computed live; the word after "an" is the ref's field name.
9. `information_model`, when given on a non-dispatch slot, must still be one of the three;
   it is required only for `dispatch`. `share_tag_format` must be `v1` or `audited`.
   `arm_budgets_ms` values must be positive ints, and the DEFAULT arm's key
   (`candidates=spatial`) is refused as "not an arm" (the default arm uses `perf_budget_ms`).
   `PolicySpec.arm_budgets_ms` is a read-only mapping; the catalog renders it as a dict.
10. A component's catalog `description` is the first paragraph of its class docstring
    (no new class attribute); `version` is the class attribute `version="1"` (or `None`
    for an injected class without one).
11. Tests: every dummy `@policy(slot="dispatch", ...)` in this branch's tests now passes
    `information_model="central"` (required by message 16): `test_policy_framework_registry.py`,
    `test_policy_catalog.py`, `test_policy_battery_catches_broken_policies.py`,
    `test_dispatch_capture_and_bench.py`, `test_policy_context_company_spatial.py`.
    Beyond the spec's list I added `test_ref_declaration_errors_are_registration_errors`
    (params) and `test_every_policy_default_tree_resolves` (registry; named in §5 C1's
    params bullet), and listed `framework.components`, `framework.catalog` and
    `dispatch.legacy` as leaves in `test_policy_import_cycles.py`.
12. This-branch tests whose contract changed with the slot table:
    `test_policies_spec_block.py::test_unknown_slot_rejected` (known-slot list) and
    `::test_legacy_market_slot_points_to_planner_market` → `test_market_component_is_not_a_slot`;
    `test_policy_framework_registry.py::test_registering_into_a_legacy_slot_...` →
    `test_market_components_are_not_slots`, plus the lookup/discovery tests' legacy lines;
    `test_container_logistics_solver.py::test_get_solver_none_is_the_slot_default` →
    `test_get_solver_none_is_the_legacy_leaf_default` (that function was written on this
    branch, `959b056`, in a pre-existing file).
13. `openride/policy_cmd.py` still has its `legacy`-status rendering branch; no real
    catalog produces that status any more, and `tests/test_cli_policy_group.py` feeds it
    a fake catalog. Left for C4, which rewrites `policy list` (components, info-model column).

### C2

1. **One TEMPORARY compatibility switch, `dispatch/profile.round2_tree_active()`** (true
   once BOTH `Partitioned` and `PooledMarket` are registered, i.e. from C3 on). Until then
   the current runtime stays round 1, byte-identical (golden 93 green):
   - `resolve_profile_policies`: a LEGACY profile resolves to its flat leaf (no
     `wrap_legacy_leaf`), so the legacy strictness flips (unknown topology / market
     component / `max_rounds`) are not yet visible to the runtime, and the pre-existing
     fail-soft tests that C3 edits still pass unedited;
   - `framework/resolve._unnamed_type`: the slot default `Partitioned` when registered,
     else `legacy.LEGACY_LEAF_DEFAULT` (the round-1 behaviour);
   - `assert_assignment_profiles_resolvable` runs `check_mirrors` only when the switch is
     on (`validate_cooperation_world` always runs);
   - kept round-1 entry points: `build_dispatch_policy` (now `= build_policy_tree`, called
     by `app.__init__`), `resolve_for_instance` (now via `bind_instance`; an unregistered
     instance is still NOT checked against the profile, as in round 1; called by
     `runner.build_runner_for_app`), and `mirror_dispatch_params` (called by
     `spec_compile` and `scenario_overrides` until C4 switches them to
     `mirror_dispatch_selection`).
   **C3 must delete** the switch's three branches and the two runtime entry points in the
   same commit that registers `Partitioned`/`PooledMarket`: the moment both register,
   every legacy profile resolves to the tree (today's `app.py` would then build the
   composite), and `check_mirrors` refuses every bundle compiled by today's compile
   (flat `policies.dispatch` + `planner.topology: "partitioned"`, message 13). So either
   C4's compile change (nested block, `mirror_dispatch_selection`) lands together with
   C3's registration, or the bundles C3's tests load are recompiled first. Tests exercise
   the tree now through stand-ins (`tests/policy_tree_standins.py`, registered under the
   real names only when those are absent, inside `isolated_registry()`), so they run
   unchanged against the real classes after C3.
2. **Name validation choice (legacy leaf):** `legacy_market_params` validates component
   names itself (`framework.components.ref_target`, imported inside the function), so
   the error names the legacy key: `planner.market.offer: unknown market.offer component
   'OfferAl'. Registered: OfferAll, OfferNone, OfferSpare. (List them: openride policy list
   market.offer)`. `wrap_legacy_leaf` does NOT validate the leaf entry (its caller already
   translated it; the resolver validates). Beyond the spec, `legacy_market_params` also
   refuses a malformed block (non-object market/role/params, a role key other than
   `type`/`params`/`version`, a non-string type) that `resolve_market_config` silently
   tolerated; a missing/empty role type is still the kind's default. `planner.topology`
   `""` is now loud (it used to mean partitioned); `None`/absent is partitioned.
3. `legacy_topology(planner, *, where="assignment profile")`: `where` is message 6's
   prefix, so C4's preprocess can say `spec`. Extra exports: `PARTITIONED`,
   `POOLED_MARKET`, `MARKET_ROLES`, `LEGACY_MAX_ROUNDS_DEFAULT` (20) and
   `LEGACY_MAX_ROUNDS_RANGE` ((1, 100)); a test pins them to `pooled_planner`'s constants
   (this leaf cannot import them).
4. `market_echo(params)` takes the typed params or their `to_dict()`, keeps
   `resolve_market_config`'s key order (offer, claim, arbitration, max_rounds) and DROPS
   `None` params (an unset nullable such as `OfferSpare.keep_below_km`), so a legacy
   component reader keeps its own default and the echo reads back to the same params.
5. **`bind_instance(node, instance, *, use_spatial_matching=True) -> (policy,
   bound_node)`**: it also returns the bound node, because an unregistered leaf replaces
   the profile's leaf node (the runner must run with it), and the legacy candidate rule
   needs the profile's `use_spatial_matching`. Re-binding a built tree whose innermost
   instance is unregistered is accepted under the same rule; an unregistered
   `CompositePolicy` is refused. Mismatches name both trees by label (`AssignmentApp._solver
   is a Partitioned/RandomAssignment but the profile resolves dispatch to
   Partitioned/GreedyNearest.`; a leaf-vs-leaf mismatch reads exactly as in round 1).
6. **Composite contract:** a composite is a dispatch policy whose dispatch ref is named
   `inner` (`framework.resolve.INNER_PARAM`); `label`/`leaf` and the tree follow only that
   param. `dispatch_node` refuses a composite without an `inner` param and
   `build_policy_tree` refuses a composite class that is not a `CompositePolicy` (checked
   at build, not at registration: the registry is a leaf and C1's registration is
   unchanged). `CompositePolicy(params=None, *, inner, inner_node)` records each injection
   on itself AND forwards it to `inner`.
7. `ResolvedDispatch` new fields (all defaulted, so hand-built nodes keep working):
   `information_model`, `share_tag_format`, `label`, `leaf`, `inner`; `label`/`leaf` are
   derived in `__post_init__` when not given; property `innermost`. `dispatch_node(name,
   params, *, timing, source)` (exported) builds the typed tree; an inner node carries the
   slot's timing and the top's source. A composite node's `candidate_kind` is
   `candidate_kind_for(its params)` (= `sampled`; the inner node carries the leaf's).
8. `ResolvedPolicy` new fields: `label`, `leaf`, `information_model`, `share_tag_format`
   (dispatch only) and `typed` (the `Params` instance, `compare=False`), plus
   `profile_params()`. `params` stays the plain dict (refs as `{type, params}`);
   `to_profile()` and the stamp emit refs as `{type, version, params}`. The stamp entry
   gains `label`, `leaf`, `information_model` (every slot) and `share_tag_format`
   (dispatch). The `Policies effective:` console line is unchanged (sim main is C4).
9. Nested merge is `framework.resolve.merge_params(spec, current, override, *, path)`
   (exported): a bare string on a ref means `{type: X}` (the CLI form), a changed ref type
   resets that ref's params, the same type merges recursively; undeclared keys pass
   through to the resolver. `parse_policy_cli_args` is untouched (nested CLI parse is C4).
   Message 5's hint is appended to `from_mapping`'s unknown-key error for every unknown
   key the inner declares (inner type from the raw value, else the ref default).
10. `drop_undeclared_legacy_params` now filters recursively into ref values (the inner and
    the components, each against its own target), for C4's legacy spec path; the profile
    path already filtered both, so it drops nothing twice.
11. `mirror_dispatch_selection` also sets an EXISTING `planner.deployment.type` to the leaf
    (it is a mirror of the leaf), and creates `planner` only when the echo is not `None`.
    `check_mirrors` checks `strategy`, `planner.topology` and (PooledMarket top only)
    `planner.market`; not `planner.deployment`. Texts of the strategy and market variants
    of message 13 (the spec gives only "the same shape"):
    `profile strategy='X' disagrees with its compiled policies.dispatch (<label>, whose
    innermost solver is <leaf>). The compiled policies block is authoritative; change a
    run's solver with --solver X (ORSIM_SOLVER is translated into exactly that). Fix the
    bundle or recompile.` and `profile planner.market disagrees with its compiled
    policies.dispatch (<label>): <k>=<a> vs <b>, …. The compiled policies block is
    authoritative; change a run's market with --policy-param dispatch.<key>=<value>
    (ORSIM_OFFER_POLICY, ORSIM_CLAIM_POLICY, ORSIM_ARBITRATION_RULE and
    ORSIM_MARKET_MAX_ROUNDS are translated into exactly that). Fix the bundle or
    recompile.` The topology variant names the top (`(Partitioned)`), as in message 13.
12. `DispatchWorld` is `@dataclass(frozen=True, eq=False)` with fields `structure`, `rule`,
    `component_of`, `max_travel_time_pickup`, `accounting_params` (a read-only snapshot;
    `assignment_cost` reads it identically); `allowed`, `own_allowed`,
    `accounting_cost`, `partners`, `may_serve`, `cooperation_pairs` are methods.
    `validate_cooperation_world` checks the ACTIVE structure only (the one the run
    executes, post-override), both adjacency directions, over
    `pools.pools_from_structure` (authored pools, else one per edge).
13. **v1 accounting equivalence (documented, and proved by the golden replay test):** the
    old partitioned scan priced `bucket_trucks` of the owner that were not in THIS
    bucket's result and passed `rule.allowed`. For an owner truck on an owner order
    `haulier_matches` holds, so `rule.allowed == pair_allowed(mtt) == world.own_allowed`;
    all of the owner's trucks sit in the owner's bucket (a cross pair is only allowed
    along an edge, inside one component), so the tick-wide committed set restricted to
    them equals the bucket's used set; `assignment_cost` returns a float >= 0 (never NaN),
    so `min` over raw costs equals `owner_reserve_km`'s float scan. The one difference:
    a RAISING cost propagated in v1 and is now accounted as `inf` (logged with
    `logger.exception`), as the claim path always did.
14. `assignment/accounting.py` is also listed in `test_policy_import_cycles._EXTRA`, and
    `dispatch/composite.py` in its `_LEAVES`.
15. **Tests.** New: `tests/test_policy_legacy_translation.py` (25),
    `tests/test_share_accounting.py` (8), `tests/test_dispatch_world.py` (10),
    `tests/test_policy_tree_resolution.py` (10; messages 1, 2, 4, 5, nested merge, compiled
    round trip, stamp, recursive legacy filter, the unnamed-dispatch compat) and the helper
    `tests/policy_tree_standins.py`. This-branch edits: `test_dispatch_profile_resolution.py`
    (+5: `test_bind_leaf_instance_at_innermost`, `test_bind_composite_instance_at_top`,
    `test_bind_mismatch_names_the_tree`, `test_unregistered_leaf_uses_legacy_kind_rule`,
    `test_mirrors_written_and_checked`), `test_policy_run_overrides.py::test_stamp_shape`
    (the stamp gains `label`, `leaf`, `information_model`, `share_tag_format`),
    `test_policy_import_cycles.py` (item 14). **Not done here:** the spec's extension of
    the cache-rebuild test ("a `planner` patch rebuilds the runner") needs the C3 cache key
    in `app._dispatch_runner`; app.py is out of C2's scope. No pre-existing (non-branch)
    test was edited.

### C3

1. **The C2 compatibility path is gone.** `profile.round2_tree_active`,
   `build_dispatch_policy`, `resolve_for_instance` and `mirror_dispatch_params` are
   deleted; `framework/resolve._unnamed_type`'s fallback is deleted (an unnamed dispatch
   is the slot default `Partitioned`, strictly) together with the `framework/resolve.py`
   → `dispatch.legacy` import (C1 review #5; a test asserts no framework module imports
   `policies.dispatch`). `assert_assignment_profiles_resolvable` always runs
   `check_mirrors`. New runtime entry point: `profile.bind_profile_instance(prof,
   instance) -> (top, bound node)`; `use_spatial_matching` (was `_use_spatial_matching`)
   and `as_dispatch_node` (was `_as_node`) are public.
2. **Runner API.** `DispatchRunner.run(ctx) -> TickOutcome(pairs, notes)` accepts any
   scope: on the tick scope a violation is message 18 (logged by `runner`), on a
   sub-scope context (only offline fixtures do that) message 19 (logged by `compose`,
   counted in the runner's stats). `runner.tick_context(...)` builds the tick;
   `share_tag_format` comes from the bound top node. `partition_context`,
   `company_context`, `company_rule` are gone from the runner (`compose.ScopeDecider`
   has the last two). `build_runner_for_policy` builds the whole default tree, so the
   battery and bench can run the composites; `accounting_params_for(selection)` is the
   offline F1 rule (first declaring node, pre-order).
3. **Context additions beyond §2.1:** `ctx.capture` (the runner's capture hook,
   inherited like `stats`) and `ctx.rule` (kept). `scopes.partition_context` /
   `company_context` keep their round-1 keyword signatures (`policy=` gives the
   rng/tiebreak/rebates, exactly the §2.3 values) plus `parent=`; without a parent they
   build an offline scope (world `None`, fresh stats), which the round-1 unit tests use.
   `TICK_SCOPE_KEY = "tick"`.
4. **Capture stays format /1, per decided SUB-scope** (C5 writes /2): the hook rides on
   the tick context, `decide_in_scope` calls it around every group/company decision,
   and the recorder names the INNERMOST node (`bound.innermost`), so every live capture
   replays exactly as before. A whole-tick (central-leaf) decision is not captured until
   C5; `run()` captures only a non-tick context.
5. **Battery/fixtures/bench, minimal:** `Fixture.context(runner)` is a partition scope
   whose parent is the runner's tick context over the same documents (world: no
   structure, the fixture's rule; a `("component", n)` fixture maps its rule's hauliers
   to component n), so a composite can decide it. The battery: `params.defaults_validate`
   builds the default tree; `order_independent` / `no_module_rng` are skipped when ANY
   node of the arm's selection declares `order_sensitive` / `module_rng` (the C5 rule,
   needed now because `PooledMarket`'s default inner `RandomAssignment` is
   order-sensitive); the violation filter also covers `compose`'s logger. Bench reads
   `.pairs`. `policy_golden.json.gz` gained `Partitioned` and `PooledMarket` entries
   (generator `--policy`, Greedy/Random entries byte-identical; C5 replaces the file).
   Per-arm budgets are C5; `perf_budget_ms` measured: Partitioned 520, PooledMarket 540.
6. **Tick-scope sampling cap (explainer gap 1): per haulier.** On the tick scope
   `sampled_trucks()` caps each haulier's trucks independently
   (`helpers.candidates.sampled_trucks_per_haulier`, first-seen haulier order, one
   `rng.sample` per haulier over the cap, drawing from `ctx.rng`); documented as
   `context.TICK_SAMPLE_SCOPE = "per_haulier"`, not added to the stamp (that would
   change the stamp shape C4 owns). Group/company scopes are unchanged (byte identity).
7. **`Partitioned` does not forward `set_rng`/`set_tiebreak`** (C2 review NIT): it
   records them on itself only, so its groups always draw from the inner's own RNG (the
   module on every live path). `set_rebate_book` still forwards. `CompositePolicy`'s
   docstring says so. `PooledMarket` injects into its inner explicitly
   (`self.inner.set_rng(ctx.rng)`, `self.inner.set_tiebreak(ctx.tick_seed)`).
8. **`PooledMarket` company solves are called directly, never through `ctx.execute`**
   (explainer gap 3 / FIX-12 decision): the pre-v2 pooled path never used the worker
   thread, so byte identity keeps it; `Partitioned` groups go through `ctx.execute`
   (one call per group, as before). The market components are built once per instance
   through `framework.components.instantiate_component` from the typed params (C1
   review #6); the fail-soft getters stay as library functions, used by no runtime
   module (AST test).
9. **Crash containment** (explainer gap 2): a non-contract exception from a top-level
   solver or a `Partitioned` group propagates out of `assign()` (pre-v2 partitioned
   behaviour; `_share_tags` stays reset); only a market company crash is contained
   (message 20 via `ScopeDecider.report_crash`, called inside the engine's `except`,
   counted as `stats["crashes"]`).
10. **The haulier-scoping "0 matches" ERROR now also fires on the pooled path** (it used
    to return before it). It is a framework check over candidates/orders, solver-
    independent; logged once per app, no RNG, no published-string change.
11. **Pooled engine:** `PooledMarketResult.share_tags` is replaced by `pair_notes`
    (cross-company awards only, `{pool_id, market_round}`); `owner_reserve_km` and
    `_finite_or_none` are deleted from `pooled_planner` (they live in `accounting`);
    `PooledPlannerContext` lost `pair_allowed_own`/`pair_allowed_pooled`. `max_rounds`
    in the notes is the validated param (equal to the old clamp on valid configs).
12. **app.py:** `_dispatch_tick` (new) holds the §2 flow and `_log_cooperation` the two
    INFO lines; `_tick_seed` and `_dispatch_tick` import `policies.dispatch` locally
    besides the three accessors (comment and an AST test say so). `_direct_call` is a
    module helper (`ctx.execute` without tpool).
13. **Compile switch (C2's ordering hazard), as minimal as consistency allowed:**
    - `preprocess._normalize_policies` wraps the translated legacy leaf through
      `legacy.wrap_legacy_leaf(leaf, planner_cfg)` (PolicyError -> SpecValidationError);
      `Preprocessor.dispatch_leaf_entry` (shape-only descent of `params.inner`; a
      built-in decomposition without `inner` has `LEGACY_LEAF_DEFAULT`) feeds the
      `strategy`/`solver_params`/`deployment` mirrors and the recipe `solver`. The
      authored `solver`/`solverParams` checks compare with the innermost solver and name
      its path (`policies.dispatch[.params.inner].type`), so the flat-block messages are
      unchanged. Messages 7/8 (authored topology/market conflicts) are NOT added (C4).
    - `spec_compile` mirrors AUTHORED blocks with `mirror_dispatch_selection` (legacy
      translations were built from those keys); `_refuse_deployment_contradicting_policies`
      compares with the innermost node and names its path; `_patch_assignment_profiles`
      asserts `strategy == leaf`. `authored_after_resolution` filters a legacy echo's
      undeclared params at every level of the tree (`_keep_declared`).
    - `frontend_scenario_spec`: `normalize_solver` accepts leaves only; the editForm
      `solver` and the legacy synth path read the innermost solver. (The rest of its C4
      list is not done.)
14. **Per-run overrides (needed so every override path stays at baseline):**
    - `apply_policy_overrides` re-mirrors with `mirror_dispatch_selection` (C2 review
      finding 3); `apply_solver_override` returns the leaf.
    - TRANSITIONAL `scenario_overrides.leaf_retargeted`: a dispatch override whose `type`
      names a LEAF while the current top is a decomposition sets the innermost solver
      (with the entry's params). This keeps `--solver`/`ORSIM_SOLVER` (still folded into
      `dispatch.type` by the sim main and the control plane) meaning "the per-scope
      solver" instead of silently becoming a central arm. Consequence until C4 separates
      the channels: `--policy dispatch=GreedyNearest` over a decomposition also sets the
      inner, not a central top.
    - `merge_policy_overrides`: a CHANGED top type keeps the current per-scope solver as
      the new decomposition's `inner` unless the override names one (`_carried_inner`;
      C2 review finding 2), so `--policy dispatch=PooledMarket` and
      `ORSIM_PLANNER_TOPOLOGY=pooled` give the same tree (test).
    - `parse_policy_cli_args` implements the §3.3 path rule (longest known slot prefix,
      non-final segments are refs, `slot.ref=Name` sets a ref's type) with both message 21
      texts ("Known slots" lists implemented slots). `market.offer.x=` is no longer a slot
      path.
    - `apply_planner_topology_override` / `apply_market_override` keep their fail-soft
      returns (C4 makes them loud) and additionally rewrite a COMPILED `policies.dispatch`
      (`_retarget_compiled_decomposition`, `_patch_compiled_market`) and re-mirror, so the
      `ORSIM_PLANNER_TOPOLOGY` / `ORSIM_OFFER_*` env path still works on bundles compiled
      with a `policies` block (legacy bundles keep working through the translation). A
      market component TYPE change resets that component's params (was: kept).
    - Sim main: `dispatch_override_label` labels the node the override reaches
      (`_overridden_node`: a leaf type, or params that only touch `inner`), so `--solver X`
      keeps `· X` and `dispatch.inner.k=v` gives `· X(k=v)`. The `Policies effective:`
      line is unchanged (it now prints `dispatch=Partitioned ...`; the label is C4).
15. **C2 review fixes:** `check_mirrors` also compares every `solver_params` /
    `planner.deployment.params` key with the first node declaring it (finding 1; message
    13 variant "profile solver_params disagrees with its compiled policies.dispatch
    (<label>): k=a vs b. ... one dial ..."); the strategy variant advises `--solver X`
    only when X is a registered leaf (finding 4); a legacy profile's resolution error
    gets the suffix naming the legacy keys (`_LEGACY_SOURCE_NOTE`, finding 5); a
    non-object `planner` is loud (`legacy._check_planner`, finding 6); a padded market
    component name is loud (preprocess still strips it at compile, the bundle reader
    does not); `max_rounds: null` is the default in `legacy` as in preprocess; the split
    `dataclasses` import is merged; `tick_seed` has one source (`world.tick_seed`).
    **Finding 7 (round-1 flat bundle without a `planner.topology` echo) — decided: not
    detectable, documented.** A round-2 compile of an authored central leaf writes
    exactly that shape (`mirror_dispatch_selection` removes the topology echo), so only
    a bundle-format marker could tell a hand-edited round-1 bundle from it. Every
    round-1 compile wrote `planner.topology`, so such bundles are refused (message 13);
    deleting the key by hand is outside what the run-start check can catch. A format
    marker is left for the user/C4 to decide.
16. **Catalog:** dispatch entries carry `inner_param` (`PolicySpec.inner_param`, the
    single descent rule used by `selection_chain`, `dispatch_node`, message 5's hint,
    `leaf_retargeted`, the label and the topology rewrite; `registry.INNER_PARAM` is
    the contract constant, re-exported by `resolve`). A test asserts the catalog default
    is one of its policies (C1 review #4).
17. **Tests.** New: `tests/test_whole_tick_dispatch.py` (the 9 spec tests plus
    `test_partitioned_groups_skip_truckless_and_haulierless_orders_like_the_inline_bucketing`
    (the golden ticks exercise neither skip) and
    `test_get_solver_harness_still_builds_the_default_tree_around_its_leaf`; 19 cases) and
    `tests/test_policy_round2_c3_fixes.py` (14: the C1/C2 review and explainer items).
    Spec-listed pre-existing edits: `test_assignment_pooled.py` (fail-loud market names;
    topology tests on `legacy.legacy_topology`), `test_pool_housekeeping.py`,
    `test_assignment_pooled_convergence.py`, the via-`__init__` golden (innermost leaf),
    the `test_assignment_pooled_benefit.py` docstring. This-branch tests forced by the
    new contracts: `test_dispatch_plan_and_invariants.py`, `test_random_spatial_candidates.py`,
    `test_policy_context_company_spatial.py`, `test_dispatch_capture_and_bench.py`
    (contexts from `scopes`, `run().pairs`, message 19); `test_dispatch_profile_resolution.py`
    (bind helpers, tree assertions, the cache-rebuild test extended with `planner`, the
    C2 TEMPORARY assertion removed, Partitioned's non-forwarding); `test_policy_tree_resolution.py`
    (slot default, carried inner); `test_policy_legacy_translation.py` (non-object
    planner, `max_rounds: null`, padded names); `test_policy_fail_loud.py`,
    `test_policy_catalog.py` (`inner_param`); `test_policies_spec_block.py` (legacy
    translation is `Partitioned{inner}`: the C4-listed names
    `test_legacy_solver_translates_to_partitioned_inner`,
    `test_every_shipped_spec_translates_to_partitioned_with_its_solver`);
    `test_policy_override_equals_compiled_edit.py`, `test_policy_run_overrides.py`,
    `test_policy_review_fixes.py`, `test_policy_param_changes_behaviour.py` (nested
    `dispatch.inner.*` paths, the topology arm through the override, the CLI parse);
    `test_frontend_editform_policy_slots.py` (nested `policySlots`, editForm `solver` is
    the innermost solver); `test_policy_import_cycles.py` (new modules, app's
    local-import rule); `tests/policy_tree_standins.py` (docstring only). Every new
    invariant test was mutation-checked (33 mutations, all caught, files restored by
    hash).
18. **Not done here (C4/C5):** authored topology/market conflict messages 7/8 and the
    `spec` variant of message 6; carry rules; `run_dispatch_overrides` with messages 9-12
    (the transitional `leaf_retargeted` and compiled topology/market rewrites stand in);
    loud topology/market env converters (still fail-soft, `max_rounds` still 1..10 on the
    env path); control `precheck_override_names`; CLI (`openride scenario new --solver X`
    and the dashboard still author a FLAT `policies.dispatch` block, which now compiles
    to a CENTRAL leaf: C4/C6 must make them write `{type: Partitioned, params: {inner:
    X}}` or descend); `policy list` components/info-model columns; the stamp's new
    fields and the `Policies effective:` label; capture /2, whole-tick fixtures, per-arm
    budgets, `LeakyMarket`/`scope_log` battery checks (C5; `scope_log` is recorded
    already). The C2 review's suggestion to move `INNER_PARAM`/`selection_chain` into
    `dispatch/` and let solvers declare `pair_note_keys` is not done (post-C3 design).

### Review fixes after C3 (framework)

These fix the early review's findings 1, 2, 5, 7, 11 and 15 (backend). The tests are
in `tests/test_policy_framework_review_fixes.py` (21 cases). Each one was
mutation-checked: 20 mutations, all caught, files restored by hash.

1. **#1. A nested dispatch ref fails loud on missing `requires`.**
   - `PolicySpec.require_available(*, where=None)` is now the one place that words a
     missing dependency. The top level (`resolve._resolve_entry`) calls it unprefixed,
     so its text is byte-identical to before.
   - The `dispatch` ref resolver (`components._resolver`) calls it with the ref path:
     `policies.dispatch.params.inner: Dispatch policy 'X' requires module(s) ...`.
   - `ref_target` itself does NOT check `requires`. It also serves listings (message
     2's leaf list, the catalog), and an unavailable leaf must still show up there.
2. **#2. Ref-default cycles. Decision: registration refuses them, a resolution guard is
   the backstop, and the catalog reports bad entries.**
   - **Registration:** `registry._check_ref_default_cycles` walks the default
     `kind="dispatch"` refs through the registered policies plus the one being
     registered, so a self-reference is caught too. A cycle is refused at the
     registration that closes it, with
     `@policy(...): ref param defaults form a cycle (A.inner -> B.inner -> A), ...`.
   - **Resolution:** `params._DEFAULT_STACK` (a ContextVar) holds the `(kind, name)`
     refs whose params come from defaults. Meeting one again raises `PolicyParamsError`
     naming the cycle. This covers what registration cannot see: component kinds, which
     are not `@policy`-registered, and specs mutated behind the registry's back.
     Explicit finite nesting is not tracked and still resolves.
   - **Catalog:** `policy_catalog()` and `component_catalog()` no longer let one entry
     blank the catalog. An entry whose schema or defaults fail with a `PolicyError` (an
     unknown or unavailable default target, a bypassed cycle) is listed with
     `available: false`, `error: <message>`, an empty-object `params_schema` and `{}`
     defaults, and is logged at ERROR. Healthy entries are unchanged. Discovery errors
     still propagate, as documented.
   - The FE `depth` backstop (review #2) is not backend work and is left to the FE.
3. **#5. Generality.**
   - **(a) Component kinds.** `framework.components.register_component_kind(kind,
     registry_getter, default, description)` (exported from `framework`) is how a solver
     side declares a kind. `offer.py`, `claim.py` and `arbitration.py` call it at import,
     after their registry and DEFAULT constants, and the descriptions moved there
     verbatim.
     - `ComponentKind` is now `(kind, default, description, registry_getter, module)`.
       The old `module`/`registry` string fields are gone, and `live_registry(kind)`
       calls the getter.
     - The framework still finds the built-in kinds when nothing has imported them.
       `components.BUILTIN_COMPONENT_MODULES` is the ONE list of declaring modules. It
       is imported lazily on the first kind lookup: through `COMPONENT_KINDS` (now a
       live read-only Mapping) or through `params._REF_KIND_LOADER` when a ref kind has
       no resolver yet.
     - Listing order is that list's order, then declaration order, so it does not
       depend on import order (tested both ways, in a subprocess).
     - This is an explicit import list, not a package scan, so the "auto-discovery in
       exactly one function" rule (house style §2.12) still holds.
     - The declaring module comes from `registry_getter.__module__`. Re-declaring from
       the same module with the same default and description replaces the getter (a
       reload). Anything else raises `PolicyRegistrationError`. `default` must be in
       the registry, and `dispatch` is not a component kind.
     - The leaf rule holds: no framework module imports `assignment.*` at import (the
       import-cycle tests and a subprocess probe).
   - **(b) The v1 reservation.** It is `registry.V1_SHARE_TAG_BUILTINS =
     frozenset({("dispatch", "Partitioned")})`, the one documented allowlist; `_V1_OWNER`
     is gone. Message 15 is unchanged. It is keyed on `(slot, name)`, not the class:
     one class backs one name, and discovery registers the built-in first, so a foreign
     class claiming the name fails in `_store`. The test stand-ins also keep working.
   - **(c) No `dispatch.legacy` import in the framework.** C3 had already removed it.
     It is now locked by an AST test over imports AND string constants, and by a
     subprocess check that importing `framework.components` does not load the module.
4. **#7. Ref `version`. Decision: validated and drift-logged; it still never selects
   anything.**
   - `params.version_echo` accepts `None` (absent) or a non-blank `str`. Anything else
     (a number, bool, object, list, `""` or whitespace) is
     `PolicyParamsError: <path>.version must be a non-empty string or null ...`.
   - `params.warn_version_drift` logs at WARNING with the prefix
     `POLICY VERSION DRIFT` (`params.VERSION_DRIFT_PREFIX`) when a compiled version
     differs from the registered one. The registered version still runs and is stamped,
     as §3.1 specified ("ignored on read" now means "not used to select").
   - Applied to:
     - nested refs (`params._resolve_ref`);
     - a `PolicyRef` passed in code;
     - the top level of a compiled `profile.policies` block (`dispatch/profile.py`:
       shape in `_authored_from_compiled`, drift after resolution; the only profile.py
       edit);
     - `instantiate_component`.
   - WARNING, not ERROR, because the run is still well-defined: a recompile fixes the
     record.
5. **#11. CLI hint.** `params._cli_path` matches `_CLI_ROOTS` (`profile.policies`,
   `policy overrides`, `policies`) by string prefix, so
   `profile.policies.dispatch.params.inner` gives `--policy-param dispatch.inner=X`. A
   path under any other root (`planner.market...`, `defaults of X...`) returns `None`,
   and message 4 then ends after the spec.json example, with no CLI clause. Message 4
   for `policies.dispatch...` is unchanged.
6. **#15. Backend nits.**
   - The "misplaced absolute import in `resolve.py`" had already gone with C3's
     `dispatch.legacy` removal. Nothing was left to move.
   - `instantiate_component` now refuses `value.params` that is not the named class's
     `Params` (`PolicyParamsError`) and validates and drift-logs `value.version`.
7. **Two C2 review items, re-checked here.**
   - **`tick_seed` single source: already done in C3.** `dispatch/world.tick_seed` is
     the only formula. `AssignmentApp._tick_seed` delegates to it, and `PooledMarket`
     reads `ctx.tick_seed`, which the app computes through it.
   - **Split `dataclasses` imports.** C3 had already merged the one in
     `assignment/accounting.py`. The only remaining split was `framework/params.py`
     (round 1): merged into `import dataclasses`.
8. **Test edits:** only the `test_policy_catalog.py` docstring that pointed at the
   removed `components._KINDS` table.

### C5

1. **`dispatch/runner.py` is edited** (not in the C5 file list; capture /2 needs it).
   `DispatchRunner.run` now calls the capture hook for EVERY context (the `kind !=
   "tick"` guard is gone), so a whole tick is captured around its decide.
   `build_runner_for_app` builds `CaptureRecorder(settings, resolved=<the bound
   tree>, rebate_aware=CaptureSettings.rebate_aware_of(prof))`. `compose.decide_in_scope`
   still calls the hook around each sub-scope (unchanged file); `CaptureRecorder` and
   the battery's recorder ignore non-tick scopes. The sim main is unchanged: `/2`
   needs nothing more from the env (`policy_capture.dir/run_id` as before).
2. **Capture /2 field names.** `horizon_steps` (the spec's "horizon"). `selection` is
   the bound `ResolvedDispatch` in stamp form: `name, version, label, leaf,
   information_model, share_tag_format, timing, params` (refs `{type, version,
   params}`). `module_rng_state` is `[version, [625 ints], gauss]`. `live_heads` are
   the RAW plan's heads (captured before `check_plan`, as in /1). `rebate_aware` is
   read with `AssignmentApp._rebate_aware`'s rule, duplicated as
   `CaptureSettings.rebate_aware_of` (app.py is not mine). Replay refusal text for
   `rebate_aware: true` (the spec gives none): `dispatch fixture at step <n> was
   captured with planner.rebate_aware: true; the rebate book is not captured, so the
   tick cannot be replayed exactly.` Message 22 is raised by `capture.check_fixture`,
   used by `read_fixture`, `context_from_fixture`, `replay_heads` and so `load_fixtures`.
   Removed /1 helpers: `restored_rngs`, the per-scope `fixture_path(dir, step, key,
   round)` (now `fixture_path(dir, step)`); new: `FORMAT_1`, `check_fixture`,
   `fixture_world`, `restored_module_rng`, `selection_stamp`.
3. **Fixtures.** `Fixture(id, seed, trucks, orders, structure=None,
   max_travel_time_pickup=None, settings, time_step=0, run_id=None,
   accounting_params={})`; `run_id=None` means `fixture:<id>` for the tick seed; empty
   `accounting_params` means the selection's own dials (`accounting_params_for`, the
   F1 rule), as before. The world is built by `DispatchWorld.from_profile` on a
   profile carrying the structure (`fixtures.world_profile`), and fixture structures
   come from `datagen.hauliers.normalize_cooperation` (`cooperation_structure`), so they
   have the compiled shape (edges, adjacency, components, pools). The round-1 fields
   `rule`, `partition_key`, `tiebreak_seed` are gone. `seeded_module_random` moved here
   (the battery re-exports it); `Fixture.module_random()` seeds the module per decide.
   `missing_haulier`: 4+3 `h_a` documents plus 2 trucks and 2 orders with no
   `haulier_id` anywhere. New `MULTI_COMPANY_FIXTURES = (tied_costs, three_company_chain)`.
4. **Battery ids.** `ARM_CHECKS` gained `information.scopes_respect_model` and
   `performance.scale` (both per arm, `scale` is the last arm check);
   `information.model_declared` and `params.rejects_bad_ref` are default-arm only. So a
   policy has 27 default checks plus 20 per non-default arm (Greedy/Random/Partitioned
   47, PooledMarket 187). `scopes_respect_model` adds one rule beyond §2.4: for a
   `per_group`/`per_company` selection every COMMITTED head must lie inside one logged
   sub-scope (a non-central policy that commits a head decided outside every scope
   decided centrally); a `per_company` scope's company is its key (`company:<h>`).
   `central` passes with "no sub-scope restriction". `rejects_bad_ref` tries an
   unregistered target on every ref and, where `leaf_only`, every registered
   decomposition. Determinism runs on `tied_costs`, `three_company_chain` and
   `scale_30x20x2` (plus `scale_200x100x2` for `reproducible`); there is no tiebreak
   dimension any more (the tick context always carries `tiebreak_seed = tick_seed`).
   `no_module_rng` no longer excludes `scale` (tick-scope sampling draws from
   `ctx.rng`). A ref arm is `{param: {"type": T}}`, labelled `param=T`
   (`battery.arm_label`, public). `decide_seeded(runner, fixture, *, trucks, orders)`
   lost `seed`/`tiebreak`.
5. **`SlowUndeclaredArm`** fails `performance.scale[variant=x]`, not `[candidates=x]`:
   a `candidates` value must be a real candidate kind or the runner refuses to build.
   The test also proves that declaring `arm_budgets_ms={"variant=x": 2100}` clears it.
6. **Budgets** (measured 2026-10-21, whole-tick `scale`, 2 x 5 runs, load avg 5.8-6.5,
   numbers at the decorators): GreedyNearest `candidates=sampled` 1865-1929 ms ->
   **5790** (above STEP_SETTLE_SECONDS, noted); Partitioned default 175-217 -> **660**
   (was 520), `inner=GreedyNearest` 110-130 -> 500; PooledMarket default 156-159 ->
   **500** (was 540), OfferNone 500, OfferSpare 620, ClaimIfGainExceeds 500, ClaimNone
   620, HighestBenefit 500, OwnerFirst 500, RandomAward 500, `inner=GreedyNearest` 580.
   GreedyNearest and RandomAssignment DEFAULT budgets were not re-declared (not in the
   C5 list). Flag: this session's RandomAssignment default worst was 212.2 ms (3x =
   637 > its 550); the battery passes (best of 3 ~175 ms), but by the rule its budget
   should be re-measured.
7. **Bench.** Extra column `label` (the selection, e.g. `PooledMarket/GreedyNearest`),
   because two aliases of one policy are otherwise indistinguishable; `policy` is the
   alias. Columns: `policy, label, info_model, fixtures, assigned, orders,
   assign_rate, deadhead_km_total, deadhead_km_mean, dual_cycle, cross_company,
   benefit_km_total, solve_ms_p50, solve_ms_max, violations`. `benefit_km_total` sums
   `owner_reserve_km - awarded_cost_km` over the shares where both are finite.
   Overrides are the nested `--policy-param` shape merged over the policy's defaults
   with `framework.resolve.merge_params`; the control's `parse_bench_params(entries,
   args)` parses `ALIAS.PATH=VALUE` with the §3.3 path rule; a repeated alias is
   refused. `load_fixtures` sorts by step number.
8. **Policy golden format 2** is `{"format": 2, "policies": {name: {version, cases:
   [{fixture, heads}]}}}`, one case per whole-tick fixture (10 per policy, no tiebreak
   variants). `tests/golden/policy_golden.json.gz` is deleted in the working tree
   (not staged).
9. **Tests.** Edited (all written on this branch, `21bd28e`; no pre-branch test was
   edited): `test_policy_contract_battery.py` (4 built-ins, arm lists, < 120 s),
   `test_policy_battery_catches_broken_policies.py` (+3 cases, `_first_feasible` gains
   `allowed=`), `test_dispatch_capture_and_bench.py` (rewritten for /2 and the bench
   columns), `test_policy_golden_versions.py` (v2). New: `test_policy_arm_budgets.py`.
   Unedited but now exercising a tick context: `test_policy_context_company_spatial::
   test_partition_scope_builds_spatial_pairs_on_demand` (its name is stale; it passes)
   and `test_policy_battery_structure_via_check_plan.py`. Every new invariant test was
   mutation-checked in a scratch mirror (17 mutations plus a golden-entry drop, all
   caught, files restored and compared by hash).
10. The golden gate is 94 now (93 + the one `3d7e84e` added).

### Review fixes after C3 (runtime)

These fix the C3 review's findings 2 (performance), 3 (docstring), 4, 5 and 6. Tests:
`tests/test_policy_round2_c3_review_fixes.py` (18 cases) and the restored F16 test in
`tests/test_pool_housekeeping.py`. 13 mutations, all caught, files restored and compared
by hash. Files: `assignment/app.py` (outside the merge zones), `assignment/accounting.py`,
`policies/dispatch/{world,context,scopes,compose,runner,profile}.py`.

1. **#2. The tick check no longer repeats physical work a sub-scope already proved.**
   - `DispatchWorld.allowed(t, o)` is exactly `structure_allows(t, o) and physical(t, o)`.
     `structure_allows` is "own haulier, or a structure edge"; physical is
     `pair_allowed_ignoring_haulier` with the world's pickup limit. A test pins the
     identity over real tick documents.
   - `compose.decide_in_scope` records a passing sub-scope plan's heads in the tick's
     `verified_heads` set. The set is created by `scopes.tick_context` and inherited
     like `stats`.
   - A head is recorded only when two things hold. First, the check really was
     `rule.allowed`. Second, `world.proves_physical(rule)` holds: the rule is an exact
     `PartitionRule` or `CompanyRule` with the world's `max_travel_time_pickup` (same
     type and value). Both rules end in the physical predicate on every branch.
   - The tick scope's feasibility is `world.tick_allowed(verified_heads)`. For a hit it
     re-checks only `structure_allows`; everything else gets the full `world.allowed`.
   - Keys are `(id(truck doc), id(order doc))`, not ids. A duplicate id with other
     content is therefore never a hit.
   - **Still authoritative.** A central or custom solver's heads are never recorded,
     so they get the full rule (tested: one physical call per head). A sub-scope with a
     WIDER structure than the world's cannot widen the tick, because the structure half
     is always re-checked (tested with cargo serving acme). A sub-scope with another
     pickup limit vouches for nothing (tested).
   - **Accounting.** `account_shares` builds the owner index once, over FREE trucks
     only (`_free_trucks_by_haulier`). Before, every share re-filtered the owner's whole
     fleet against the commits: on the pooled golden tick that was 165 shares x ~100
     committed trucks.
   - **No cost memo.** Within one call each `(truck, order)` is priced at most once:
     the reserve prices owner trucks for the share's order, the award prices the
     carrier's truck, and no two shares carry one order. A memo would have added
     overhead and saved nothing.
   - **Component timings** (median of 15 ticks, ms, at `2a1ccf2` -> fixed):

     | case | tick `check_plan` | `account_shares` |
     |---|---|---|
     | pooled chain | 2.40 -> 0.57 | 2.12 -> 1.10 |
     | partitioned chain | 2.04 -> 0.53 | 3.35 -> 2.04 |
     | nocoop sampled | 0.65 -> 0.18 | 0.05 -> 0.05 |

   - **Gate 6.** Timing script over `tests.golden.harness.run_case`. Each process does
     3 repeats per case after a warm-up; 12 rounds interleaved old/head/fixed, so n=36
     per arm. Old is a scratch worktree at `ba74fbe` (removed afterwards). Load average
     (1 min) over the batch: min 5.3, median 6.1, max 12.8.

     | case | `ba74fbe` | `2a1ccf2` | fixed | ratio fixed/old | limit |
     |---|---|---|---|---|---|
     | A-greedy-partitioned-chain-spatial-cap500 | 222.3 | 219.4 | 217.0 | 0.976 | 235.4 PASS |
     | A-greedy-pooled-chain-spatial-cap500 | 315.0 | 316.6 | 316.2 | 1.004 | 332.8 PASS |
     | C-greedy-partitioned-nocoop-only-sampled-cap40 | 151.6 | 152.0 | 150.9 | 0.996 | 161.1 PASS |

     An earlier fixed-vs-`ba74fbe` batch (n=36, load 5.6-9.5) gave 0.978 / 1.007 /
     0.987. **No waiver is needed.**
   - The review's +5.7% pooled offset did not reproduce for `2a1ccf2` on this box today
     (1.005). The direct component saving is ~2.9 ms per pooled tick and ~2.8 ms per
     partitioned chain tick. That is below the run-to-run spread of a ~300 ms
     `run_case`, which includes the harness's deepcopy of the tick. So the medians
     cannot resolve it (CLAUDE.md §6.24: measure the component).
2. **#3. The `context.py` sampling docstring is corrected.**
   - The tick-scope cap covers `cap` trucks per company. That equals `PooledMarket`.
   - It equals `Partitioned` only when every group is one company.
   - On the golden chain tick at cap 40, central samples 160 and `Partitioned` 80. A
     test pins exactly these numbers.
   - The stamp field (`candidate_settings.tick_sample_scope`) and the feature-doc note
     stay C4/C7.
3. **#4. A stale reused top is REBUILT, not raised.**
   - `_bound_reused_tree` returns `(bound, current)`. `current` is False when a reused
     composite's `params` differ from the bound node's, or its `inner_node` differs from
     the bound inner (dataclass equality).
   - `bind_instance` then rebuilds every composite from the bound tree, around the SAME
     innermost leaf instance (`_rebuilt_around_leaf`). It carries the old top's injected
     rng, tiebreak and rebate book.
   - `AssignmentApp._dispatch_runner` makes the rebuilt top `_solver` (re-keying the
     cache), so `_inject_rebate_book` reaches the tree that runs.
   - **Why rebuild rather than raise.** Pre-v2 market components followed a profile
     patch (the original F16 test asserted exactly that), and §8 risk 3 is about a
     CLASS mismatch, which still raises.
   - **Why leaves are reused, not compared.** The runner hands a leaf the bound params
     as `ctx.params`. Every library leaf decides from those, so a leaf's constructor dict
     never runs stale. Many tests inject `get_solver(X, None)` (params `{}`) and assert
     the innermost identity. A third-party leaf that reads `self.params` instead of
     `ctx.params` is outside the contract.
   - The F16 test now builds `_solver` as the full tree. It asserts the instance's
     `params["max_rounds"]` and its instantiated offer component, besides
     `resolved.params`. The new tests assert behaviour: `round_stats()["max_rounds_backstop"]`,
     no shares under OfferNone, and a patched Partitioned inner deciding exactly like a
     fresh app.
4. **#5. `solve_in_thread` reaches every top-level solver.**
   - `DispatchRunner._decide` calls `ctx.execute(policy.decide, ctx)` unless the top is
     a `CompositePolicy`. A decomposition owns its sub-scopes' offloading, so nothing is
     wrapped twice: `Partitioned` makes one call per group, `PooledMarket` none (pre-v2).
   - Tested with a counting stub: central 1, Partitioned 2 (the chain tick's groups),
     PooledMarket 0, pairs identical with the flag off.
   - Golden-neutral: `direct_execute` is a plain call.
5. **#6. The haulier-scoping ERROR skips a contract-event tick.**
   - `_dispatch_tick` records `_tick_contract_events`, this tick's growth of
     `stats["violations"] + stats["crashes"]`.
   - `assign` skips the "haulier scoping blocked everything" ERROR when it is non-zero,
     so the one-shot `_warned_haulier_block` stays armed.
   - Tested: a rule-blind central tick logs message 18 only, and a real block on the
     next tick still fires.
   - The alternative ("warn only when ids are missing") was rejected because it would
     silence a real block by mismatched but present ids.
6. **Test edits:** `tests/test_pool_housekeeping.py::test_market_components_pick_up_a_behavior_patched_after_construction`
   (restored, see 3). No other existing test changed.

### C4

1. **Run-name segment formats (sim main; `scenario_overrides.dispatch_override_label`).**
   The dispatch label is built ONLY from `ORSIM_POLICY_OVERRIDES` (`--policy`/
   `--policy-param`/`--policy-overrides`) and `ORSIM_SOLVER`; the topology variable keeps its
   own segment (appended at its old place, after the structure override) and the market
   variables add none, as before. A node of the selection's `inner` chain is *touched* when
   the request names its type (`--solver` names the innermost one) or sets one of its own
   params. Then:
   - only the innermost per-scope solver touched: `<Leaf>` / `<Leaf>(k=v,...)` (the pre-v2
     `--solver X` segment, unchanged): `--solver X` → ` · X`;
     `dispatch.inner.dual_cycle_bonus_km=0` → ` · GreedyNearest(dual_cycle_bonus_km=0.0)`;
   - any node above it touched: the whole chain, `/`-joined, each node `<Name>` or
     `<Name>(k=v,...)`: `--policy dispatch=PooledMarket` → ` · PooledMarket/GreedyNearest`
     (the stamp label); `--policy dispatch=PooledMarket --policy-param dispatch.max_rounds=5
     --policy-param dispatch.offer.keep_below_km=3 ...` →
     ` · PooledMarket(max_rounds=5,offer=OfferSpare(keep_below_km=3.0))/GreedyNearest(dual_cycle_bonus_km=0.0)`;
     a component ref renders as `<Type>` / `<Type>(k=v)`;
   - `(k=v,...)` lists only the params the request set on that node, keys sorted, values the
     validated value as compact JSON (the round-1 rule).
   Both shapes are what the FE parser classifies (`Name(...)`, and `A/B` whose every part is
   one). **Known limit:** a central leaf (`--policy dispatch=GreedyNearest`) and the per-scope
   `--solver GreedyNearest` both read ` · GreedyNearest` in the run NAME; the stamp
   (`label`, `information_model`) tells them apart, and the FE prefers the stamp. Legacy
   inputs reproduce `ec085f5` byte for byte: verified by driving `ec085f5`'s own `main()` and
   this branch's with the same harness (`test_legacy_run_env_overrides.py`, compiled and
   legacy bundles).
2. **`run_dispatch_overrides` builds ONE override entry per behavior and merges it once**
   (not four sequential merges), so the steps compose: the topology step sets the top (and,
   for a new `PooledMarket`, `planner.market` under what the request stated), the market
   step sets component refs (a same-type ref keeps its params), and the solver step does a
   trial merge to find the innermost path, then names the type there. Every behavior is
   validated before any is mutated. It returns `(merged_overrides, labels)`; nothing
   requested returns `({}, {dispatch: None, topology: None})` and mutates nothing.
   Additions beyond the spec: a market variable that contradicts a type/value the explicit
   overrides set is refused (`ORSIM_OFFER_POLICY='OfferSpare' conflicts with
   dispatch.offer.type='OfferAll' from ORSIM_POLICY_OVERRIDES ...`; no spec text);
   `ORSIM_MARKET_MAX_ROUNDS` accepts an int or an integer string (the env form), never a bool
   or float; an unknown `--solver` is `--solver 'Nope': Unknown dispatch policy 'Nope'. ...`;
   an unknown topology is message 6's env variant `ORSIM_PLANNER_TOPOLOGY='x' is not a legacy
   topology (...). The decomposition is now the dispatch policy: --policy
   dispatch=Partitioned or --policy dispatch=PooledMarket.` Message 12 uses the spec's
   `--solver` wording on the env path too (one text). `apply_solver_override`,
   `apply_planner_topology_override`, `apply_market_override` are wrappers with their old
   returns; `leaf_retargeted`, `_retarget_compiled_decomposition`, `_patch_compiled_market`
   and `_rewrite_compiled_dispatch` are deleted.
3. **A top-type change now carries the current inner when the override sets only the
   inner's params** (`framework.resolve._carried_inner`; the CLI's `scenario_cmd._carried_inner`
   follows the same rule). C3 carried it only when the override did not mention `inner`
   at all, so `--policy dispatch=PooledMarket --policy-param dispatch.inner.dual_cycle_bonus_km=0`
   resolved the params against the DEFAULT inner (RandomAssignment) and failed. An override
   that names the inner's type still replaces it.
4. **Recipe planner echo:** under an AUTHORED `policies.dispatch` the recipe's `planner` is
   the raw authored value verbatim (`None` when absent); under a legacy translation it stays
   the normalised block with the deployment mirror (unchanged bytes for every shipped spec).
   `_recipe_planner(planner_cfg, raw_planner, provenance)` replaces the authored-deployment
   argument (the authored deployment is part of the raw planner).
5. **Messages 7/8 live in `legacy.check_authored_planner`** (preprocess calls it with the RAW
   spec planner; `PolicyError` → `SpecValidationError`). Message 8 compares the resolved
   `PooledMarket` params of both sides through the registry (defaults filled, so an authored
   `{}` market agrees with defaults-only params) and lists every disagreeing leaf path
   (`offer.type='OfferAll' vs 'OfferSpare', offer.params.keep_below_km=None vs 3.0, ...`;
   comparing two different component types lists their params too). A `policies` side that
   does not resolve is left to the resolver's own message. An empty `topology: ""` reads as
   absent (the compile's falsy rule). The `spec` variant of message 6 is not added:
   `_normalize_planner` already refuses an unknown topology first with its pre-existing
   `planner: unknown topology ...` text, which two pre-existing tests match.
6. **Bundle-format marker (C2 review #7, C3 deviation 15's open item): `profile.policies_format
   = 2`** (`legacy.POLICIES_FORMAT_KEY`/`POLICIES_FORMAT`), written by
   `spec_compile._patch_assignment_profiles` and by every per-run override commit
   (`scenario_overrides._commit`). `legacy.check_policies_format` refuses a profile that has
   `policies` without it (message: `profile.policies has policies_format=None, not 2: it was
   compiled before round 2, when a flat policies.dispatch {type: X} meant the per-scope solver
   X under planner.topology; it now means a CENTRAL X. Recompile the scenario ...`); a legacy
   profile (no `policies`) needs none. The check runs at run start in the sim main and in the
   control dry run through `scenario_overrides.assert_policies_format`, NOT inside
   `profile.assert_assignment_profiles_resolvable`: `profile.py` was owned by the runtime
   review-fix agent during C4. Folding the call in there (one run-start check) is a one-line
   follow-up. Every main-tree bundle has no `policies` and passes.
7. **`candidate_settings.sample_scope`** is stamped (C3 review #3): `per_haulier` for a
   `central` top (the tick scope, `context.TICK_SAMPLE_SCOPE`), `per_group` for Partitioned,
   `per_company` for PooledMarket, mapped from the top's `information_model`
   (`sim main.sample_scope_for`).
8. **Control:** `build_run_policy_overrides(policies, params, overrides_json)` lost its
   `solver` argument; `validate_run_policy_args` = build → `precheck_override_names(overrides,
   solver)` (new, `scenario_overrides`; the name/params part is
   `framework.check_named_overrides`, which validates every NAMED type and ref with the
   merge's paths and skips refs whose type is not named, since their params belong to the
   bundle's selection) → bundle → `run_dispatch_overrides` → format and run-start checks. With
   neither overrides nor a solver the bundle is not read. The payload shape is unchanged:
   `{"ok": true, "action": "validate_run_policies", "overrides": {...}}` /
   `{"ok": false, "action": "validate_run_policies", "message": "..."}` (+ `commandId`);
   `overrides` is the `ORSIM_POLICY_OVERRIDES` object and never contains `--solver`.
   `openride run`/`run --json` now hand `--solver` to `SimulationRunner(solver=...)`
   (`ORSIM_SOLVER`), as the control plane's `run-simulation` already did.
9. **CLI.** `policy list [SLOT|KIND]` lists slots and the three component kinds (a kind name
   lists only that kind) with an `Info model` column (`composite (inner)` under a
   decomposition); `policy show SLOT|KIND NAME` renders a ref param as `ref → <kind>`
   (`(leaf)`), its default type and the names it accepts, plus model / share tag format /
   module RNG. `solver list` is `policy list dispatch` restricted to the leaves (no longer
   byte-equal to it; its test was updated). The round-1 `legacy` slot rendering is removed.
   `scenario new/edit --solver X` never writes a flat leaf: flags rebase nested over the
   current block (`merge_params`, carried inner on a top change), then `--solver` sets the
   innermost solver (new with no selection: `{type: Partitioned, params: {inner: {type: X}}}`;
   a central top is replaced; the same solver keeps its params; a flags-stated different
   type there, or a decomposition name, is refused). `new --spec-file` of a legacy spec rebases
   on its translation and drops the dials it supersedes (`drop_dispatch_dials_superseded_by`).
   `known_solvers()` (frontend_scenario_spec and scenario_overrides) lists leaves only.
10. **Fixes found on the way:** `spec_compile._profile_policies` folded a legacy
    `use_spatial_matching: false` into the TOP's params, which under the round-2 translation
    is `Partitioned` (no `candidates`), i.e. silently not at all; it now folds into the
    innermost solver. The editForm display fallback (a spec that does not compile) is nested
    (`{type: Partitioned|PooledMarket, params: {inner: {type: <raw solver>, params: {}}}}`).
11. **Tests.** New: `tests/test_legacy_run_env_overrides.py` (11),
    `tests/test_validate_run_policies_order.py` (3). This-branch edits:
    `test_policies_spec_block.py` (+5 spec-listed/extended: pooled translation, messages 7
    and 8, recipe verbatim, format marker; I1 now nested over both topologies; the stale
    deployment case uses `Partitioned{inner}` because the legacy recipe's `topology:
    partitioned` would now trip message 7 first), `test_policies_spec_carry.py` (+1),
    `test_policy_run_overrides.py` (env helper renamed to `_apply_dispatch_overrides_or_exit`,
    message 12, `--solver` innermost / composite (11), sample scope, the `Policies
    effective:` line, CLI `--solver` nested, the control payload without the solver),
    `test_policy_override_equals_compiled_edit.py` (paths `dispatch.inner.*`, compile cases on
    `Partitioned{inner}` plus central and PooledMarket recompiles, the PooledMarket per-run
    case), `test_policy_review_fixes.py` (`build_run_policy_overrides` signature, payload
    without solver), `test_frontend_editform_policy_slots.py` (nested fallback; the compiled
    case authors `Partitioned{inner}` because the smoke spec's `topology: partitioned` would
    trip message 7 next to a central leaf), `test_cli_policy_group.py` (round-2 fake catalog
    with components, model column, ref rendering, `solver list` leaves), `test_policy_catalog.py`
    (`known_solvers` leaves), `test_policy_fail_loud.py` / `test_cli_policy_group.py` (the
    renamed sim-main helper in the source-order asserts), `test_policy_round2_c3_fixes.py`
    (the C3-transitional `--solver` assertion now goes through `run_dispatch_overrides`).
    `test_assemble_spec_key_registry.py`: a pre-existing FILE, but the edited lines are the
    `policies` sentinel this branch added in round 1 (`b98d452`); its flat
    `{type: GreedyNearest}` next to the sentinel planner's `topology: pooled` now trips
    message 7, so it is respelled as the same round-1 selection in round-2 form
    (`PooledMarket{max_rounds: 7, inner: GreedyNearest(3.25)}`). Nothing pre-branch changed.
    **Pre-existing test edited (spec-listed only):** `test_scenario_market_overrides.py`:
    `fail_soft` → `fail_loud`; `patches_only_loaded_behaviors` (type change resets params,
    legacy-shape mirror); `rejects_out_of_range` (0, 101, "x" raise; 11 applied);
    `accepts_unknown_algorithm_names` → `rejects_unknown_component_names`; module docstring.
    Mutation checks: 39 mutations over the new invariant tests (files restored by hash),
    37 caught; the two survivors (removing `_set_innermost`'s same-type early return, and
    making `_merge_ref` reset a same-type ref) each leave the other mechanism holding the
    "same solver keeps its params" assertion, which that test's docstring states instead of
    claiming teeth.

### Performance (gate 6)

**Where the overhead was.** cProfile diff of `tests.golden.harness.run_case` (277e67b vs
572f4e4, 10 run_cases per case), confirmed by perf_counter wrappers on the coarse
components. The per-tick framework layers are NOT where the milliseconds went:
world 0.02 ms, tick/partition contexts 0.01-0.03 ms, `check_plan` 0.6-2.6 ms (sub-scopes)
+ 0.2-0.5 ms (tick), `heads_to_pairs` <0.2 ms, `account_shares` 0.05-2.0 ms (replaces the
old inline owner-reserve scan, which cost about the same), the `_dispatch_runner` cache
key's three `json.dumps` 10 us. The gap was the PER-PAIR cost chain inside GreedyNearest,
called ~16-26k times per tick: `decide.<locals>.cost -> ctx.dual_cycle_cost` (kwargs, two
`_unwrap` isinstance calls) `-> objective.dual_cycle_cost` (a new knob dict per call)
`-> constraints.assignment_cost -> _is_dual_cycle_pair` (the round-1 factor-out), four
Python frames and a dict where 277e67b had one closure (`app._cost`) and one call.

**Changes (behaviour-preserving; golden 94 green, no fixture or harness change):**

1. `constraints.assignment_cost` inlines the dual-cycle predicate again (the pre-round-1
   body, verbatim). `_is_dual_cycle_pair` stays as the predicate behind
   `objective.is_dual_cycle`. The two copies are pinned separately:
   `test_dual_cycle_cost_equals_assignment_cost_on_a_grid` now also asserts
   `is_dual_cycle` against a verbatim oracle predicate. Teeth checked: `d <= radius_km`
   -> `d < radius_km` in either copy fails that test.
2. `objective.dual_cycle_cost_fn(*, bonus_km, radius_km)`: the knobs are bound once and
   the returned closure is one frame above `assignment_cost`, the same depth as the old
   `app._cost`. Same function, same two floats, so each value is bit-identical to
   `ctx.dual_cycle_cost`. GreedyNearest's `decide` uses it. The pairs it scores are always
   raw documents (`candidate_pairs`, `sampled_trucks`, `raw_orders`), so it unwraps no
   views. `ctx.dual_cycle_cost` is unchanged for authored policies.

Not changed, on purpose: `check_plan` runs on every scope and every tick as before (no
skip for central or custom solvers), and the `_dispatch_runner` cache key (10 us, and its
`json.dumps` is what detects an in-place profile patch). The harness builds a fresh app,
and so a fresh runner (0.4-0.8 ms, mostly `params.declared_params -> typing.get_type_hints`),
for every case. A live run builds it once. A per-class `declared_params` cache would
remove that time but would serve stale results to a test that mutates a Params class,
so it was left out.

**Components, ms per run_case (n=20, `gc.collect()` before each sample, load 4.6-5.1):**

| component | A-part 277e67b / before / after | A-pooled | C-sampled |
|---|---|---|---|
| run_case | 184.1 / 192.8 / 183.4 | 280.9 / 293.4 / 284.4 | 137.8 / 147.5 / 139.6 |
| greedy `solve_pairs` (cost loop) | 76.0 / 82.9 / 74.6 | 125.2 / 134.3 / 125.0 | 125.5 / 133.9 / 129.1 |
| `check_plan` scope + tick | - / 2.5 / 2.4 | - / 3.1 / 3.1 | - / 0.8 / 0.8 |
| `account_shares` | - / 2.1 / 2.0 | - / 1.0 / 0.9 | - / 0.05 / 0.04 |
| runner build (harness only) | - / 0.42 / 0.42 | - / 0.74 / 0.75 | - / 0.41 / 0.41 |

**Gate 6, interleaved, 3 arms (277e67b / 572f4e4 / this change), 14 rounds x 5 = 70 samples
per arm per case, rotating arm order:**

| protocol | case | 277e67b | after | ratio | bound | verdict |
|---|---|---|---|---|---|---|
| perf1.py as written, load 4.5-5.8 | A-partitioned | 203.5 | 206.0 | 1.012 | 215.6 | PASS |
| | A-pooled | 281.0 | 300.4 | 1.069 | 297.1 | FAIL |
| | C-sampled | 138.3 | 141.2 | 1.021 | 147.2 | PASS |
| `gc.collect()` before each sample (the timeit convention), load 8-13 | A-partitioned | 183.7 | 185.6 | 1.010 | 194.9 | PASS |
| | A-pooled | 277.4 | 281.0 | 1.013 | 293.3 | PASS |
| | C-sampled | 138.5 | 140.7 | 1.016 | 147.4 | PASS |

572f4e4 under the same perf1.py batch: 1.041 / 1.095 / 1.080.

**The remaining A-pooled FAIL is the GC phase, not per-tick work.** Each run_case
samples bimodally: +25-35 ms when a full (gen-2) collection lands inside it. Measured with
`gc.callbacks`: both arms run 0.60 gen-2 collections per run_case in steady state (18 in
30 runs each) and 69-71 gen-0 collections per run_case. Within one mode the pooled delta is
+6.1 ms (low mode, 277.9 -> 284.0) and -1.3 ms (high mode, 309.5 -> 308.2). perf1.py takes
only five samples per process, so a process's gen-2 phase decides which mode the median
lands in. That phase is set by everything allocated before the timed loop, imports
included. 277e67b's pooled window caught a gen-2 in 10 of 70 samples, this change's in 27,
which moves the median by a whole mode. Changing the protocol flips the verdict:

- one `gc.collect()` after the warm-up (GC cost stays inside every sample): A-pooled
  1.046, PASS (bound 294.8, after 291.7);
- two warm-ups instead of one: A-pooled 1.020, PASS.

One real, smaller effect remains in the pooled path: at the same phase it promotes 12%
more objects to gen 2 (48,967 vs 43,518 per run_case; about 5k more (truck, order)
candidate tuples are alive at a gen-1 collection). That raises the short-window gen-2 hit
rate but not the steady-state rate. The source is the extra per-company-scope objects
(context, Plan, id_index, the `heads_to_pairs` tuples). Removing them would mean letting
the commit reuse the solver's pair tuples instead of mapping heads back through
`id_index`, which is a contract change. It was not attempted.

**Verdict, against the spec's baseline 277e67b (lead).** Under the spec's protocol as
written (perf1.py, five samples per process), gate 6 PASSES for A-partitioned (1.012) and
C-sampled (1.021) and FAILS for A-pooled: 1.069x, +19.4 ms per tick against a bound of
+16.1 ms. This is recorded as a **waiver for the pooled case**, not as a pass. With the GC
phase held level (one `gc.collect()` before each sample) the same pooled case is 1.013x,
+3.6 ms per tick. That is the best estimate of the real per-tick cost of the whole-tick
layers on the pooled path, which builds per-company contexts, plans and id indexes. The
earlier "no waiver" entry under "Review fixes after C3 (runtime)" measured against
ba74fbe (round-2 C2), not against the spec's 277e67b, and is superseded by this one.

### Final review fixes (runtime)

1. **MAJOR-4: the partition scope's sample RNG is a required argument.**
   - `scopes.partition_context` takes `sample_rng` as a required keyword with no
     default. It was hardcoded to the `random` module, so no composite built on the
     shipped helpers could pass `determinism.no_module_rng` without declaring
     `module_rng`. `None` still means "draw from this context's `rng`", as on the tick
     scope, but a caller has to pass it explicitly.
   - `Partitioned._group_context` passes the `random` module, with a comment giving the
     reason: the pre-v2 inline `random.sample` and the golden's module-RNG lock. Its
     `module_rng` declaration now covers a draw its own code names. The golden still
     passes, 94/94.
   - A third-party composite passes `sample_rng=ctx.rng` and calls
     `inner.set_rng(ctx.rng)` once per tick, which is the pattern `PooledMarket` uses.
     It then passes the battery's `no_module_rng` with no declaration.
   - The `rng=` half of the review's suggestion was not added. The context's `rng` is
     still `policy.rng`, and the existing `set_rng` injection already lets a composite
     choose it. A second channel for the same value would only add a way for the two to
     disagree.
   - The composite how-to (`docs/policy_authoring.md`) is not edited here because
     another agent owns it. It should say: pass `sample_rng=ctx.rng` and inject
     `ctx.rng` into the inner.
2. **MAJOR-5 / FIX-12: PooledMarket company solves go through `ctx.execute`.** This
   corrects C3 deviation 8. The final review calls it "C3 deviation 9"; it is item 8
   under "### C3". It also corrects the "PooledMarket none (pre-v2)" line in "Review
   fixes after C3 (runtime)" #5.
   - `compose.ScopeDecider.run` now calls
     `company_ctx.execute(decide_in_scope, self.policy, self.node, company_ctx)`. The
     company context inherits `execute` from the market's context.
   - `DispatchRunner._decide` still does not wrap a composite, so no offload is nested.
   - The "byte identity keeps it direct" argument for deviation 8 was wrong:
     - With the flag off, `execute` is `direct_execute`, a plain call. Every golden case
       runs this way.
     - With the flag on, `tpool.execute` is synchronous, so the order of company solves
       and every RNG draw are unchanged.
     - The golden still passes, 94/94.
   - With `solve_in_thread` on, a heavy inner (NetworkFlow) now leaves the eventlet hub
     for each company solve, up to `max_rounds` × companies times per tick, as
     `Partitioned` does per group.
   - Docstrings in `pooled_market.py`, `compose.py` and the `runner.py` comment are
     updated.
3. **Tests.**
   - New: `tests/test_policy_final_review_runtime.py`, 4 tests:
     - The signature of `partition_context` has no `sample_rng` default.
     - A per-group composite built on `compose`/`scopes` with the shipped
       `RandomAssignment` inner. It passes the battery's own `_no_module_rng` with no
       `module_rng` declaration when it passes `ctx.rng`, and fails it on `scale` when
       it passes the module.
     - PooledMarket on golden tick A with a counting stub tpool, for inner GreedyNearest
       and for RandomAssignment:
       - one offload per company solve (11 and 10 solves), each at depth 1, and none
         when the flag is off;
       - pairs, share tags and the `random` state after the tick are identical with the
         flag on and off.
   - Edits this change forced:
     - `test_policy_round2_c3_review_fixes.py::test_solve_in_thread_reaches_every_top_level_solver_once_per_decision`:
       the expected PooledMarket count changes from 0 to 11, and its docstring is
       updated.
     - `sample_rng=random` is added to every test call of `scopes.partition_context`,
       which keeps their previous behaviour. The affected files are
       `test_random_spatial_candidates.py`, `test_dispatch_context_helpers.py` (3
       calls), `test_dispatch_profile_resolution.py`, `test_policy_round2_c3_fixes.py`,
       `test_policy_round2_c3_review_fixes.py` and `test_dispatch_plan_and_invariants.py`.
   - No `policies/testing/*` call site needed a change.
   - Mutation checks, each restored and confirmed by sha1:
     - a default on `sample_rng` fails the signature test;
     - `partition_context` ignoring its argument and sampling from the module fails the
       decision-RNG case;
     - the direct company solve fails both pooled cases and the c3 PooledMarket case
       (0 offloads);
     - wrapping composites in `_decide` fails both pooled cases (12 and 11 offloads,
       depth 2) and the c3 Partitioned case (3) and PooledMarket case (12).

### Final review fixes (selection)

1. **MAJOR-1: an override can no longer create the `policies_format` marker.**
   - `scenario_overrides._profiles_with_current` runs `legacy.check_policies_format` on
     every source profile before it resolves anything. Every override path reaches it:
     `run_dispatch_overrides` (`ORSIM_POLICY_OVERRIDES`, `ORSIM_PLANNER_TOPOLOGY`, the
     market variables, `ORSIM_SOLVER`) and `apply_policy_overrides`. The sim main and the
     control-plane dry run (`validate-run-policies`) both go through
     `run_dispatch_overrides`.
   - The error has the agent id as prefix, the same shape as
     `assert_assignment_profiles_resolvable`.
   - `_commit` is a backstop. It runs the same check on every planned profile before it
     writes any of them, so it never stamps marker 2 onto a `policies` block that had no
     marker.
   - The review's `fin/marker_bypass.py` now refuses all five cases: no override,
     `--solver GreedyNearest`, `--solver RandomAssignment`,
     `--policy-param dispatch.dual_cycle_bonus_km=0` and `--policy dispatch=PooledMarket`.
   - Legacy profiles (no `policies`) and marked round-2 profiles still accept overrides.
   - **Correction to the `572f4e4` commit message.** It says the check now covers
     "agent-side" callers. No agent-side caller exists: `AssignmentApp` never runs
     `check_policies_format`. The callers are run-start only: the sim main
     (`_assert_policies_resolvable_or_exit` and, now, the override application) and the
     control dry run. No agent-side caller was added, because `assignment/app.py` belongs
     to the runtime fixes. Because the sim main gates every run, a refused bundle never
     reaches an agent.
2. **MINOR-2: a central top and a per-scope `--solver` of the same name still give the same
   run-name segment. This is documented, not changed.**
   - `--policy dispatch=GreedyNearest` and `--solver GreedyNearest` both give
     ` · GreedyNearest`.
   - The stamp tells them apart: label `GreedyNearest`/central versus
     `Partitioned/GreedyNearest`/per_group. Every round-2 run carries the stamp, and the
     frontend reads the stamp first.
   - No legacy input can produce a leaf top. A prefix keyed on the resolved top could
     therefore never change a legacy name; the 24/24 legacy-name harness would stay
     identical.
   - It is not implemented because it changes the run-name format the frontend parses.
     `lib/runNameMeta.ts` reads bare names, `Name(k=v)` and `A/B` labels, and would need
     to learn the prefix in the other repo. Doing that inside this merge buys little
     while the stamp already tells the two apart.
   - The follow-up format, if the coordinator wants it: when the RESOLVED top is a leaf (no
     `inner_param`) and a segment is emitted, write it as ` · central:<Leaf>` or
     ` · central:<Leaf>(k=v,...)`. Key it on the resolved top, not the request, so a
     central bundle plus `--solver Y` gives ` · central:Y`. The `:` is free: MINOR-3 below
     keeps it out of policy names.
3. **MINOR-3: policy names are restricted at registration, and label values are escaped
   on purpose.**
   - `registry.check_policy_name` is called from `_check_metadata` for every `@policy`. It
     requires `POLICY_NAME_PATTERN = [A-Za-z_][A-Za-z0-9_.-]*`, the frontend's
     `NODE_NAME` grammar. Any other name raises `PolicyRegistrationError`.
   - The pattern rules out every run-name separator (` · `, `/`, `(`, `)`), and also `=`,
     `,`, `:`, whitespace and non-ASCII.
     - `=` and `,` are refused because a name sits inside `Name(k=v,...)` labels next to
       them, and the frontend's split is not specified for them.
     - `:` is kept free for prefixes such as `lifecycle:` (and a possible `central:`).
     - All four built-in names match the pattern.
   - **Component names are NOT checked.** Component classes register by being put into
     plain module dicts (`OFFER_REGISTRY` and the like) that the framework reads live
     through `components.register_component_kind`. There is no registration call to hook,
     and `components.py` and those modules are outside this change's file set.
     `check_policy_name` is public, so the follow-up is to call it on each registry name
     in `register_component_kind` and in `_component_target`.
   - `scenario_overrides._label_value` now passes `ensure_ascii=True` explicitly, with a
     comment saying it is load-bearing. A test pins that ` · `, `/`, `,)`, `"` and `ü` in a
     param value come out JSON-escaped. For example `a · b` becomes `"a · b"`, so
     the segment never contains a raw ` · ` and stays ASCII.
4. **MINOR-4: `--capture-dispatch every=N` now counts dispatch ticks.**
   - `CaptureRecorder` counts the whole ticks it sees (`ticks_seen`). The first tick is
     always captured, then every N-th one. `CaptureSettings.wants(tick_index)` replaces
     `wants(time_step)`.
   - This is the less surprising of the two fixes. The field (`every_n_ticks`) and the help
     ("every N-th tick") already said ticks. On `consortium_collab_7d` (one tick every 28
     steps) the old step modulus made `every=4` capture every tick and `every=10` every
     5th tick.
   - The help in `openride/run_group.py` and `openride_control/command.py` now says "every
     N-th dispatch tick (not sim step)".
   - Caveat: a runner rebuilt mid-run (`AssignmentApp` rebuilds it when the profile is
     patched) starts counting again from zero. The `max_files` cap still counts the files
     already on disk.
   - `test_dispatch_capture_and_bench.py::test_capture_writes_fixture_when_enabled` now
     expects `step_3`/`step_6` (ticks 0 and 2), not the step-modulus `step_4`/`step_6`.
5. **MINOR-5: the compile warning reads the resolved selection.**
   - `Preprocessor._warn_pools_without_pooled_dispatch` runs after `_normalize_policies`.
     It warns only when the active structure declares pools AND the resolved
     `policies.dispatch` top (authored, or translated from `planner.topology`) is
     `Partitioned`.
   - An authored `PooledMarket` spec with no `planner` is no longer warned.
   - The wording is in `policies.dispatch` terms. The advice depends on where the
     selection came from:
     - an authored block is told to set `policies.dispatch.type='PooledMarket'`;
     - a legacy spec keeps the `planner.topology` / `ORSIM_PLANNER_TOPOLOGY` advice,
       plus `--policy dispatch=PooledMarket`.
   - The two existing tests in `test_assemble_spec_key_registry.py` now look for the new
     needle, "decomposition (PooledMarket) is OFF".
6. **MINOR-6: compile writes only authored `policies` slots into `spec.json`.**
   - `frontend_scenario_spec._spec_from_recipe` drops every `policies` slot that the input
     spec did not author before `write_spec`. A legacy spec keeps its legacy keys
     (`solver`, `planner`) and no derived block.
   - The bundle's `$.recipe` is unchanged and still carries the full block, so recompiling
     from the bundle is unaffected.
   - Gate, through the real `compile_scenario` in a tmp `ORSIM_SCENARIOS_DIR`, all 16
     shipped specs:
     - the written `spec.json` has no `policies` (16/16);
     - the profile is `Partitioned{inner: GreedyNearest}` with marker 2 (16/16);
     - compile, recompile from the written `spec.json`, and compile from `$.recipe` give
       identical truck, order, facility and assignment agents (16/16).
   - The written `spec.json` still differs from the git-tracked file for 13 of the 16 specs.
     The differences are the earlier recipe normalisation: `cooperation`, `hour_axis`,
     `referenceTime`, `facilityRules*` and `planner` defaults, and on
     `agent_pipeline_smoke` the `matrix`->`historical` alias and `source`. None of them
     is `policies`, and they are not from this round. `collab_smoke_12t`,
     `consortium_baseline_7d` and `consortium_collab_7d` are written back unchanged.
   - This also removes the second consequence the review raised: a spec compiled on
     round-1 code carrying a flat leaf that later reads as authored central. No new
     compile writes a derived block.
7. **MINOR-8: §3.3 reworded in place.** The sentence "A type change resets params to that
   type's defaults" now matches `resolve._carried_inner`. A new decomposition keeps the
   current per-scope solver as its `inner`, with its params, unless the request names the
   inner's type. The current per-scope solver is the current `inner`, or the current top
   when that is a leaf. Every other param resets. No code changed.
8. **MINOR-9: message 12 names `--policy` too.** It now reads `... from
   --policy/--policy-param/--policy-overrides; ...`. The conflicting type can come from
   `--policy dispatch=X`. `openride/scenario_cmd._refuse_solver_conflict` already said
   `--policy/--policy-param`. The exact-text pins in `test_policy_run_overrides.py` and
   `test_legacy_run_env_overrides.py` are updated. §3.7's message 12 text is not edited
   (this section is append-only); the code is the reference.
9. **MINOR-10: message 5's hint survives an explicitly named top.**
   - The case is `--policy dispatch=Partitioned --policy-param dispatch.dual_cycle_bonus_km=0`.
     The control precheck validates it before any bundle is read, so it checks against
     the DEFAULT inner, RandomAssignment, which does not declare the key.
   - When the inner's type is not stated and the default inner lacks the key,
     `resolve._inner_key_hint` now names every registered leaf that declares it:
     `'dual_cycle_bonus_km' is a param of an inner solver (e.g. GreedyNearest): use
     dispatch.inner.dual_cycle_bonus_km.`
   - When the request does state an inner that lacks the key, there is still no hint,
     because one would mislead. When the stated or default inner declares the key, the
     spec's message 5 wording is unchanged.
10. **Tests.**
    - New: `tests/test_policy_final_review_selection.py`, 26 tests (MAJOR-1 ×8, MINOR-9,
      MINOR-10, MINOR-3 ×13, MINOR-4, MINOR-6 ×2).
    - New: `test_assemble_spec_key_registry.py::test_pools_warning_reads_the_resolved_dispatch_selection`
      (MINOR-5).
    - Mutation checks, each restored with `cmp`:
      - without the `_profiles_with_current` check, 5 MAJOR-1 cases fail;
      - without the `_commit` backstop, its test fails;
      - without both, the control dry run accepts;
      - `ensure_ascii=False` fails the escape test;
      - without `check_policy_name`, 10/11 name cases fail (the empty name was already
        refused);
      - the step modulus in `before_decide` makes `every=4` capture all 9 ticks;
      - the old unconditional warning fails the MINOR-5 test (and one sibling);
      - writing the bare recipe fails the MINOR-6 legacy test;
      - the old message-12 wording fails its test;
      - without the unstated-inner hint branch, the MINOR-10 test fails.

### Final review fixes (battery)

1. **MAJOR-3: an unmeasured composite `inner=X` arm is budgeted from X's own budget.**
   - `battery.arm_budget(spec, arm)` (public, renamed from `_arm_budget`) is now the one
     function that decides an arm's `performance.scale` budget. First rule that applies:
     1. the default arm: `perf_budget_ms`;
     2. a measured arm: `arm_budgets_ms[<label>]`. An explicit key always wins, so the
        built-ins' measured `inner=GreedyNearest` budgets are unchanged;
     3. an unmeasured `dispatch`-kind ref arm (`inner=X`): `arm_budget(X, <X's params in
        the arm>)` + the composite's `perf_budget_ms`. A ref arm runs X at its defaults,
        so this is `X.perf_budget_ms + composite default budget`;
     4. any other unmeasured arm (a str choice, a component ref) still inherits the
        default arm's budget and fails loudly when slower.
   - Why the composite's default budget is the overhead: it bounds the composite's own
     work PLUS one solve of its default inner on the same `scale` tick, so the sum
     over-covers the overhead by one default-inner solve. On `scale` (one haulier, one
     company, no pools) `Partitioned` and `PooledMarket` call the inner on one scope.
     A composite that calls its inner many times on that tick must measure the arm;
     this is in the docstring.
   - The detail names the derivation, e.g. `budget 5660 ms (arm not measured: derived =
     NetFlowSketch perf_budget_ms 5000 ms + Partitioned overhead perf_budget_ms 660 ms)`.
   - Reviewer's repro, `fin/netflow_sketch.py` unchanged (NetFlowSketch declares 5000 ms):
     own 27/27, `Partitioned` 67/67 (inner arm best 2571.6 ms, budget 5660),
     `PooledMarket` 207/207 (best 2452.9 ms, budget 5500). Before: both composites
     failed `performance.scale[inner=NetFlowSketch]` under 660 / 500 ms.
   - Same script with the sketch declaring 1000 ms (below its ~2.5 s): it fails its own
     `performance.scale`, and both composites fail exactly
     `performance.scale[inner=NetFlowSketch]` (budgets 1660 / 1500 ms).
2. **MINOR-1: skips are SKIP, and the `module_rng` declaration is scoped.**
   - `CheckResult` has `skipped: bool = False` and a `status` property
     (`SKIP`/`PASS`/`FAIL`). A skip keeps `ok: true`, so nothing that counts failures
     changes and the CLI exits 0. Check ids are unchanged. A check function reports a
     skip by returning `battery.Skip(True, "skipped: ...")`, a `NamedTuple`, so a caller
     that unpacks `ok, detail` still works.
   - The control payload (`cmd_policy_validate`) adds `status` and `skipped` to every
     row and a top-level `skipped` count; the message reads `all N checks passed (k
     skipped)`. The CLI table renders `SKIP` in yellow.
   - Found while checking the table: the CLI printed the check id unescaped, so Rich
     read an arm suffix such as `[inner=GreedyNearest]` as markup and dropped it. Every
     arm row looked like a default-arm row. The id is now `escape`d, the same as the
     detail; the CLI test pins it.
   - `determinism.order_independent` is still skipped whenever a node of the selection
     declares `order_sensitive` (the declarations compose), now as SKIP.
   - On a composite's `inner=X` arm the skip detail (and the PASS detail of the scoped
     module check) ends with `X itself is checked in X's own battery (openride policy
     validate dispatch X)`.
   - **Scoping `module_rng` (decided here).** A blanket skip is no longer used. When a
     node declares `module_rng`, `_scoped_module_rng` runs one probe decision per
     robustness fixture:
     - every built node whose `rng` is the module by default (nothing injected:
       `Partitioned`'s inner) gets a private `Random(fixture.seed)` through `set_rng`.
       The inner's sanctioned `self.rng` channel then leaves the module;
     - the `random` module's draw functions are wrapped for the probe, and each draw is
       attributed to the NEAREST stack frame whose `self` is one of the selection's built
       policies. A non-tick `DispatchContext` frame on the way marks the draw as a scope
       cap sample (`sample_rng` = the module, which after MAJOR-4 only a composite can
       choose);
     - a draw is covered when its node declares `module_rng`, or when it is a scope
       sample and the node or one of its ancestors declares it. `Partitioned`'s
       declaration therefore covers its own `sample_rng=random`, and nothing its inner
       does directly.
   - Verdicts: any uncovered draw FAILS, naming the node. Covered draws, or a module
     change with no attributable draw (a function bound at import, `random._inst`),
     give SKIP: provenance cannot be separated there. A module untouched with the
     channel redirected gives PASS.
   - Built-ins:
     - `Partitioned` default arm: SKIP (1 scope cap sample on `scale`);
     - `Partitioned[inner=GreedyNearest]`: PASS;
     - `PooledMarket` declares nothing, so it runs the plain check as before.
   - Reviewer's case: `Partitioned[inner=ModuleRandom]` was "PASS (skipped)" and now
     FAILS (`ModuleRandom on all_infeasible, ...`), like `PooledMarket[inner=ModuleRandom]`.
   - Limits:
     - The attribution sees calls through the `random` module's attributes. A function
       bound at import still changes the state; when nothing else drew, that is the
       unseparable SKIP.
     - Wrapping the module is safe because the battery is single-threaded.
     - The wrapper is installed only for the probe, never in a run.
     - The probe adds one decision per fixture, only on arms with a declaration.
3. **MINOR-7: the exact-message tests build their name lists from the registry.**
   - New helper module `tests/policy_registry_names.py`: `dispatch_names()` (message 1),
     `leaf_names()` (messages 2 and 11) and `component_names(kind)` (message 3). Each
     list is read when the function is called, so it sees the active `isolated_registry`.
   - The wording stays spec-exact; only the lists come from the helper. Sites:
     - `test_policy_tree_resolution.py` messages 1 and 2. The asserts move out of the
       `pytest.raises` block, into the `decompositions()` block;
     - `test_policy_run_overrides.py` message 11;
     - `test_policy_framework_params.py` messages 1, 2 and 3.
   - `test_policy_framework_registry.py:237` is NOT changed: it clears the registry and
     registers exactly two dummies, so it never depended on the leaves. The
     `_named_dispatch_registry` tests in `test_policy_framework_params.py` clear it too;
     they use the helper anyway, for uniformity.
   - `test_policy_contract_battery._WANT_ARMS` is kept, with its intent stated in a
     comment: a new arm of a built-in must be acknowledged. It lists each built-in's own
     str-choice arms and its ref params `(param, kind)` in declared order.
     - The targets of a ref param are read from the live registry by `expected_arms`, so
       a new leaf or component adds its arm without an edit.
     - `_BUILTIN_TARGETS` still pins that every shipped target is an arm.
   - Not changed: `test_assignment_pooled.py:511` pins the offer component list
     (`OfferAll, OfferNone, OfferSpare`). It breaks only on a new offer COMPONENT, not on
     a new leaf.
   - Verification:
     - `test_the_exact_message_tests_survive_an_extra_leaf` registers `ExtraLeafZ` under
       `isolated_registry` and calls the five exact-text tests and the arm-list test for
       both composites.
     - A scratch pytest plugin (`scratchpad/bfix_plugin/extra_leaf_plugin.py`) registered
       a `ZzExtraLeaf` for a whole session. Over `tests/test_policy_*.py
       tests/test_dispatch_*.py tests/test_cli_*.py tests/test_whole_tick_dispatch.py`
       the result was 1012 passed, 1 skipped, 2 failed:
       - `test_policy_golden_versions::test_every_dispatch_policy_has_a_golden_entry`,
         by design: a new policy must capture its golden entry;
       - `test_rules_baseline_does_not_recompile`, the known baseline failure.
     - The six files named in the review alone gave 498 passed with the extra leaf, and
       the contract battery ran the extra leaf's 68 checks.
4. **Component names obey the policy-name grammar** (the selection fixes' follow-up).
   - `components.register_component_kind` runs `registry.check_policy_name` on every
     name in the registry it is handed.
   - `_component_target` runs it again on the resolved name, because the live dict can
     gain a name after the kind was declared (a test or plugin injection).
   - A bad name raises `PolicyRegistrationError` with the policy-name message. The kind
     is not declared, and the ref does not resolve.
   - The three built-in kinds' names all match.
5. **Tests.** New file `tests/test_policy_final_review_battery.py`, 18 tests:
   - MAJOR-3 ×3 (derivation and precedence; slow leaf with a declared budget keeps both
     composite batteries fully green; a leaf slower than its own budget fails both
     composite arms);
   - MINOR-1 ×5 (status; Partitioned SKIP/PASS/FAIL and the own-battery note; a
     declaring third-party composite; control payload; CLI);
   - MINOR-7 ×2;
   - component names ×7 + 1.

   The slow leaf sleeps 0.75 s on the 1000-truck tick only. It stands in for the
   sketch, whose ~2.5 s Hungarian decide would cost minutes in the suite.

   Edits to existing tests:
   - `test_policy_arm_budgets.py` (`arm_budget` rename, docstring);
   - the docstring of `test_policy_battery_catches_broken_policies.py`;
   - the MINOR-7 sites above.
6. **Mutation checks** (`scratchpad/bfix_mut.py`; each restored and confirmed by sha1):
   - no derivation in `arm_budget`: the derivation test and the green-composites test fail;
   - measured key ignored: the derivation test and `test_policy_arm_budgets`' gating
     test fail;
   - inner budget ×10: the too-slow test fails;
   - `skipped` flag dropped in `_timed`: the Partitioned test fails;
   - every draw covered (the blanket skip): the Partitioned test and the declaring
     composite test fail;
   - the inner's RNG not redirected: the Partitioned test fails (the default arm FAILS
     on RandomAssignment's sanctioned draws);
   - the order skip returned as a plain tuple: the Partitioned test fails;
   - `status` dropped from the payload: the control test fails;
   - the CLI rendering by `ok`: the CLI test fails;
   - the literal leaf list in `test_policy_tree_resolution`, or the literal
     `inner=GreedyNearest` in `_WANT_ARMS`: the extra-leaf test fails;
   - the name check removed from `register_component_kind`, or from
     `_component_target`: all 7 bad-name cases fail;
   - the check id unescaped in the CLI table: the CLI test fails.
7. **Gates** (2026-10-22):
   - golden 94 passed;
   - `tests/test_policy_*.py tests/test_dispatch_*.py tests/test_cli_*.py
     tests/test_whole_tick_dispatch.py`: 945 passed, 1 failed. The one failure is the baseline
     `test_rules_baseline_does_not_recompile`;
   - `openride policy validate dispatch X`, each exiting 0:
     - `Partitioned`: `PASS all 47 checks passed (3 skipped)`. The skips are
       `order_independent` ×2 and the default `no_module_rng`;
       `no_module_rng[inner=GreedyNearest]` is now a real PASS;
     - `PooledMarket`: `PASS all 187 checks passed (8 skipped)`, the `order_independent`
       of every arm whose inner is RandomAssignment;
     - `GreedyNearest`: `PASS all 47 checks passed`;
     - `RandomAssignment`: `PASS all 47 checks passed (2 skipped)`, `order_independent`
       on both arms;
   - the reviewer's `fin/netflow_sketch.py`: green for both composites (item 1).

### Verification review fixes

Source: `review_r2_verify.md` (MINOR-A, MINOR-B, MINOR-C, NITs) and MINOR-11 of the
final review. New test file `tests/test_policy_verification_review_fixes.py`, 19 tests.

1. **MINOR-A (`verified_heads`): a documented read-only contract, not a fingerprint.**
   - Measured on golden tick A (405 heads for `Partitioned[inner=GreedyNearest]`, 408
     for `PooledMarket[inner=GreedyNearest]`), per tick, n=200 repetitions:

     | per-head work over every head of the tick | Partitioned | PooledMarket |
     |---|---|---|
     | full physical check (`pair_allowed_ignoring_haulier`), what the shortcut saves | 1.31 ms | 1.32 ms |
     | one fingerprint of exactly the fields feasibility reads | 0.48 ms | 0.51 ms |
     | fingerprint at record + at check + compare (what a keyed shortcut costs) | 0.89 ms | 0.94 ms |

     The fingerprint covers truck `profile.{current_loc, last_known_loc, init_loc,
     home_facility_name, truck_size, restricted_areas}` and order `pickup_loc`,
     `profile.{pickup_loc, order_size, pickup_facility_name, dropoff_facility_name}`,
     `order_size`. At ~0.9 ms per tick it is over the ~0.5 ms threshold and would give
     back most of what the C3 #2 shortcut saved (the tick `check_plan` is 0.2-0.5 ms with
     it). It would also duplicate, in a second place, the list of fields feasibility
     reads, which silently goes stale when that list grows.
   - So the contract is documented instead: `DispatchPolicy`'s docstring says the
     documents (`ctx.raw_trucks`/`raw_orders`, and the pairs `candidate_pairs` yields)
     are read-only, not even mutated temporarily, and why. `DispatchWorld.tick_allowed`'s
     "Pure" claim is qualified the same way.
   - Tests: the three shipped selections leave every tick document byte-equal across a
     decision; and the boundary is pinned (a composite that moves trucks to their
     pickups for a sub-solve and restores them commits physically infeasible heads with
     a 300 s pickup limit; read-only, it commits none). If the shortcut ever gains a
     fingerprint, that second test is inverted and the contract text dropped.
2. **MINOR-B: correction to "Final review fixes (runtime)" #1.** The composite recipe
   there is incomplete. The full recipe is: pass `sample_rng=ctx.rng` to
   `partition_context`, and call BOTH `inner.set_rng(ctx.rng)` and
   `inner.set_tiebreak(ctx.tick_seed)` once per tick (as `PooledMarket` does,
   `pooled_market.py:135-136`). Without the tiebreak a `GreedyNearest` inner falls back
   to its `self.rng.shuffle` tiebreak and the composite fails
   `determinism.order_independent[inner=GreedyNearest]`. The `scopes.partition_context`
   docstring and both test composites that model the recipe
   (`test_policy_final_review_runtime.py`, `test_policy_final_review_battery.py`) now
   include the call; the new test shows the recipe passing `order_independent` and
   `no_module_rng` (undeclared), and failing `order_independent` without the tiebreak.
3. **MINOR-C: `no_module_rng` attribution.**
   - The owner of a draw is the nearest frame whose FIRST PARAMETER is one of the
     selection's built policies, by identity (`_attribute_frame`). Not a local named
     `self` (so `def decide(me, ctx)` is attributed), and not any local (a composite's
     frame that merely holds its inner in a local is never credited with the inner's
     draws, which an `isinstance` scan over all locals would do).
   - A non-tick `DispatchContext` on the way is a scope cap SAMPLE only when that context
     was handed to the node: it is a parameter of the node's entry frame (the outermost
     frame with the node as first parameter, below its parent's: `decide_in_scope` /
     `decide`). A scope a node builds for itself with `sample_rng=random` is now its own
     direct draw, so `Partitioned[inner=X]` FAILS such an X (it was a SKIP credited to
     Partitioned). Not asked for by the lead, but it was the same misleading-detail class.
   - Threads: the probe also wraps `threading.Thread.start`, so a thread STARTED during
     the decision (a `ThreadPoolExecutor` a node submits to included) inherits the
     attribution of the frame that started it. A declaring node drawing in its own
     thread is now SKIP (was a false FAIL "`<no policy frame>`"); an undeclared one FAILS
     by name. A draw with no policy frame and no spawner (a thread that existed before
     the decision) goes to the "provenance cannot be separated" SKIP bucket with its own
     detail, never a FAIL and never a PASS; X's own battery still FAILS an undeclared X.
   - Reviewer's 16 probe leaves re-run (`ver/rng/attr.py`): every row is unchanged except
     renamed-self (SKIP -> FAIL), own-scope sample (SKIP -> FAIL), the declaring thread
     (FAIL -> SKIP), and the undeclared thread, which now FAILS naming the leaf instead of
     `<no policy frame>`. Still no false PASS. The built-ins' rows are unchanged
     (`Partitioned` default SKIP, `[inner=GreedyNearest]` PASS).
4. **NITs.**
   - `legacy.check_policies_format`: the marker must be `type(marker) is int` and 2.
     `2.0`, `"2"`, `True`, `3` and any other present value are refused as "unknown
     format" (recompile with this build); only an ABSENT marker still says "compiled
     before round 2". The reviewer's MAJOR-1 matrix (`ver/m1/m1.py`) re-run against an
     archive of `f8b91b9`: the only change is that `marker-2.0` is now REFUSED on all 11
     paths in the sim and the control (it was accepted on 10). The control's no-flag
     dry run still returns `{}` without reading the bundle (NIT 5, by design; the sim
     main refuses at start).
   - MINOR-11 dead code removed: `profile._DECOMPOSITIONS` (no reader) and
     `scenario_overrides.assert_policies_format` with its two callers (sim main
     `_assert_policies_resolvable_or_exit`, control `validate_run_policy_args`).
     `assert_assignment_profiles_resolvable` already runs `check_policies_format` first,
     with the same `assignment agent '<id>': ` prefix and message, at the same point in
     both flows, so run start is unchanged. The refusal BEFORE overrides is the separate
     source check in `run_dispatch_overrides` -> `_profiles_with_current` (MAJOR-1), which
     is untouched. One contrived difference: a collection holding a non-mapping behavior
     AND a round-1 profile is now refused for the non-mapping first (both refuse, exit 2).
     `test_policies_spec_block::test_compiled_profile_carries_the_round2_format_marker`
     is retargeted at `assert_assignment_profiles_resolvable`.
   - The five test-only `scenario_overrides` entry points (`apply_policy_overrides`,
     `apply_solver_override`, `apply_planner_topology_override`, `apply_market_override`,
     `known_planner_topologies`) are kept and marked "Compatibility shim" in their
     docstrings and the module docstring; no run path calls them.
   - The control message for a green battery with skips is `N passed, k skipped` (N
     excludes the skips); without skips it stays `all N checks passed`. The CLI prints
     the payload message, so it follows. The failure message is unchanged.
   - Depth-2 offload (NIT 3): documentation only. A composite whose `inner` param is NOT
     `leaf_only` and that is given another composite (`Partitioned`) offloads at depth 2
     under `solve_in_thread`: the outer per-scope `ctx.execute` runs the inner composite,
     whose own per-group `ctx.execute` nests. Real `eventlet.tpool.execute` called from a
     pool thread runs the function directly, so this does not deadlock, and the reviewer
     measured identical pairs and RNG state flag on/off. It is not a supported shape for
     performance reasoning; declare `leaf_only=True` on an `inner` param unless the
     composite really composes composites.
5. **Mutation checks** (`scratchpad/vfix/mut.py`, PYTHONDONTWRITEBYTECODE=1, each restored
   and confirmed by sha1), all CAUGHT: attribution by `self` name; no handed-to test;
   no `Thread.start` wrapper; unattributed draws back to rogue; `tick_allowed` shortcut
   off (the boundary test); `GreedyNearest` writing to a truck doc (3 selections); the
   old marker predicate (6 marker cases); the old control message. Also: dropping
   `check_policies_format` from `assert_assignment_profiles_resolvable` fails the
   retargeted spec-block test.
6. **Gates** (2026-10-22): golden 94 passed; `tests/test_policy_*.py tests/test_dispatch_*.py
   tests/test_whole_tick_dispatch.py tests/test_cli_*.py tests/test_scenario_*.py
   tests/test_policies_*.py tests/test_legacy_run_env_overrides.py
   tests/test_validate_run_policies_order.py tests/test_assignment_*.py`: 1273 passed,
   2 failed, both baseline (`test_rules_baseline_does_not_recompile`,
   `test_rebate_aware_flag_defaults_false_in_every_shipped_scenario`);
   `openride policy validate dispatch X`, exit 0 each: `Partitioned` `PASS 44 passed, 3
   skipped`; `PooledMarket` `PASS 179 passed, 8 skipped`; `GreedyNearest` `PASS all 47
   checks passed`; `RandomAssignment` `PASS 45 passed, 2 skipped`.
