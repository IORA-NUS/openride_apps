> **Archived build plan** for the generic solver contract (WS0-WS8, 2026-10-22/23), with the
> final review's findings as appendices. `$SP/...` paths (gate harnesses, baselines, probes)
> lived in the build session's scratchpad and are NOT preserved; the durable record is
> `docs/reference/features/26-policy-slots.md` (in-repo copy `26-policy-slots.draft.md`).

# Final plan: one generic, pluggable solver contract for the `planner` slot

Implementation plan for coder agents. Supersedes `plan_generic_contract.md`; folds in
`critique_generic_contract.md` and the user's binding decisions (review page §8/§9).

- Backend: `/home/user/openride_apps_policy_slots`, branch `feat/policy-slots`, base **`35b5b92`**.
- Frontend: `/home/user/openride_server/analytics` repo, branch `feat/policy-slots`, base **`6f9ca8b`**
  (an existing worktree is at `scratchpad/fe_wt`).
- `SP` = `/tmp/claude-1000/-home-user/f13570e2-81af-483b-8703-76ae77d0783f/scratchpad`.
- Paths are relative to the backend worktree unless absolute. `file:line` refers to `35b5b92`.
- Read first: `/home/user/CLAUDE.md` §6.27 and §7, and
  `/home/user/docs/reference/features/26-policy-slots.md` (§2 boundary, §3.1 tick, §6 traps).

## 0. What the user decided (binding), in one paragraph each

1. **Ownership.** No `owner_of`, company map or haulier list is added to the tick. The haulier
   is already a field of every truck and order document (`constraints.haulier_of`,
   `assignment/constraints.py:222-230`). The tick hands over the documents (raw + typed view,
   `views.py:31-90`, whose `haulier_id` is read by that same function at `views.py:76,90`).
   Anything that needs the haulier reads it from there. The framework's ownership check reads
   the same field off the same objects. Duplicate haulier channels are removed (§3).
2. **Result = pairs.** The framework accepts `(truck_id, order_id)` pairs plus opaque per-pair
   `solver_notes`. Chains/tours are not a framework concept; a future tour solver's wrapper turns
   tours into pairs. Verified in §4: no shipped solver returns a tail.
3. Accepted page recommendations:
   - **(a) A2 + ceiling.** The top node's `bind()` declares cross-company `(owner, carrier)` pairs
     once. The framework refuses any declared pair outside a ceiling, which is the union of the
     scenario's declared partnerships over all arms. Every committed pair is checked before
     commit. Only the top node declares: an inner declaration must be a subset of the top's.
   - **(b) Measurement.** `account_shares` stays framework-owned. The dual-cycle dials keep
     "first node wins", quarantined in `compat/`. PooledMarket first builds its bid cost from its
     inner's params. Moving the dials to a world setting changes bundle bytes and override
     semantics, so it is a **documented later phase, not in this plan** (§11). The v1 tag rule
     becomes an explicit name-keyed rule in `policies/compat/`. Solvers return opaque
     `solver_notes`. A compat table keyed on note-key **names** routes `pool_id`/`market_round`
     to their old wire fields. Any other key goes to a new `solver_notes` field, which is omitted
     when empty, so the bytes are identical.
   - **(c) C1.** The cooperation menu and the active arm stay scenario world data and are passed
     opaquely. `--structure` and the Run dropdown keep patching `cooperation.active`. An unknown
     id now fails loud. Which keys a tree consumes is derived from what `bind` reads (no
     `consumes_world` field). The dashboard disables the structure dropdown, with a reason, when
     no node reads `cooperation`.
   - **(e) Metadata.** Keep `requires`, `order_sensitive`, `module_rng`, budgets and `offload`.
     Delete `information_model`; it becomes display `badge` text plus stamped provenance. Add
     `candidate_hint`. Do NOT add `consumes_world`, `pair_note_keys` or `run_stats_key`.
   - **(f)** Exactly one registered wrapper per entry. Shared adapter bases live in a
     solver-helper library (`policies/solver_lib/`) that the framework never imports.
   - **(g) Nesting.** `tick.call` with narrow-only sub-ticks, enforced by an id-subset check in
     `restrict`. The top pre-commit check is the real guarantee. Adds the candidates channel (H1)
     and the two-RNG split (H9). Nesting stays **single `inner`** (G8).
   - **Leftovers.** The v1 share tags, `pool_id`/`market_round` and the legacy translation go to
     `policies/compat/`, which may name solvers because it translates old inputs. A
     framework-vocabulary test guards this; its final allow-list is exactly `compat/`.
4. NetworkFlow is out of scope. It is not registered.

## 1. Verified ground truth (re-measured for this plan at 35b5b92)

| Fact | Value | How |
|---|---|---|
| `dispatch_golden_v1` cases | **68** (`header.case_count`=68, `len(cases)`=68); `test_dispatch_golden_identity.py` collects 70 = 68 + 2 meta | python + `--collect-only` |
| via-init golden | **15** (`test_dispatch_golden_via_init.py`) | `--collect-only` |
| published compare / versions | 9 / 7 tests; `policy_golden_v2` = 4 policies x 10 fixtures, heads, default params | `--collect-only`; file |
| Golden total | `pytest` of the 4 golden files: **101 passed** in 18 s | run |
| Out of grid | **204/204, 0 errors**: 51 profiles (17 main-tree bundles x partitioned/pooled/env-pooled) x 4 ticks. Baseline saved at `SP/oog/out_base_35b5b92.json` (profiles `SP/oog/prof.json`) | `SP/c3rev/mkprof.py` + `runcmp.py` |
| Full suite | **37 failed / 2741 passed / 5 skipped / 8 errors**. Failing set (45 lines) saved at `SP/oog/base_fail_35b5b92.txt`; identical to r4. None is in policy code (ride-hail, interaction equivalence, `test_assignment_rebate_seam::test_rebate_aware_flag_defaults_false_in_every_shipped_scenario` (needs compiled bundles), `test_cli_rules_baseline`, `test_package_all_order_exact`) | run with `--continue-on-collection-errors` (**required**: without it the 8 collection errors abort the session) |
| `openride policy validate planner X` | GreedyNearest **47 PASS**; RandomAssignment **45 + 2 skip**; Partitioned **44 + 3 skip**; PooledMarket **179 + 8 skip**; all exit 0. Saved `SP/oog/val_base_*.txt` | run |
| FE | `npx vitest run` **601/601** (46 files); `npx tsc --noEmit` **exit 0** | run in `SP/fe_wt` |
| Golden worlds | all 68 cases pass message 14 (pools ⊆ adjacency); adjacency symmetric; ceiling ⊇ active edges | python over cases |
| Main-tree bundles (17) | all legacy (no `policies` block); `solver_params` = defaults `{5.0, 0.5}`; adjacency symmetric; message 14 OK | python over `/home/user/openride_apps/scenarios/*/scenario.json` |
| Golden dials | every case's `solver_params` = `{5.0, 0.5}` = constraint defaults (`constraints.py:136-137`) = GreedyNearest defaults | python |
| Chains with tails | none shipped (§4) | grep |
| Revision baselines | skipped set at base = 5 (`SP/oog/base_skipped_35b5b92.txt`, bundle-dependent tests); per-check validate tables 47/0, 47/2, 47/3, 187/8 (`SP/oog/valtable_base/`); stamp + run-name oracle for 11 input paths x {compiled, legacy} on `collab_smoke_12t` (`SP/oog/stamp_base_35b5b92.json`, made by `stamp_harness2.py`); `mkprof2.py` reproduces `prof.json` byte for byte | run at 35b5b92 |

## 2. The final contract

### 2.1 Registry entry (`policies/framework/registry.py`, evolves `PolicySpec`, `:95-136`)

The class keeps the name `PolicySpec` to limit churn. It is "the solver entry".

```python
@dataclass(frozen=True)
class PolicySpec:
    slot: str                          # :108  "planner" (slots.py:59-71)
    name: str                          # :109  POLICY_NAME_PATTERN :245 (a run-name node)
    version: str                       # :110
    description: str                   # :111
    requires: tuple[str, ...]          # :112  importability, missing_requirements :138-159
    cls: type                          # :113  THE WRAPPER (implements SolverWrapper, §2.2)
    params_cls: type[PolicyParams]     # :114  THE schema: from_mapping / json_schema / to_dict (params.py)
    order_sensitive: str | None        # :115  battery only
    perf_budget_ms: int                # :116  battery only
    module_rng: str | None             # :119  battery only
    arm_budgets_ms: Mapping[str, int]  # :120  battery only
    refs: tuple[tuple[str, str, bool], ...]   # :121 derived (param, kind, leaf_only)
    offload: Literal["self", "children"]      # NEW (WS1). Replaces runner.py:122 isinstance(CompositePolicy).
                                              #   Default: "children" if a planner ref exists, else "self".
    badge: str | None = None           # NEW (WS4b). Display only, never read at run time (replaces
    badge_tooltip: str | None = None   #   the information_model badge; FE lib/policy-slots.ts:645-672)
    # DELETED in WS4b: information_model (:117, checks :280-288), share_tag_format (:118, :291-301)
    # KEPT derived: is_composite (:123-126), inner_param (:128-136), INNER_PARAM = "inner" (:88)

def policy(*, slot, name, version, description, requires=(), order_sensitive=None,
           perf_budget_ms=2000, module_rng=None, arm_budgets_ms=None,
           offload=None, badge=None, badge_tooltip=None) -> Callable[[type], type]
```

Registration check `_check_slot_base` (`:376-385`, `_SLOT_BASES :67-71`) is relaxed (G5). The
class must define a callable **`solve_tick`**, either its own or inherited from the helper base
`DispatchPolicy`, which serves legacy `decide(ctx)` classes. A class may still subclass
`DispatchPolicy`, but that base is optional.

**Why `solve_tick` and not `solve` (B1, verified).** `solve` is already the PAIR API:
`PairSolver.solve(trucks, orders, *, pair_allowed, cost)` is `@abstractmethod`
(`dispatch/pair_solver.py:47-56`), and `BaseAssignmentSolver = PairSolver`
(`assignment/solver/base.py`). GreedyNearest (`greedy_nearest.py:105`) and RandomAssignment
(`random_assignment.py:66`) implement it and call it from their own decision method
(`:102`, `:58`), and 7 test call sites use it directly
(`test_container_logistics_solver.py:88-136`, `test_dispatch_plan_and_invariants.py:269`).
Under the name `solve`, every PairSolver subclass would pass registration by accident, the
framework would call the pair method with a tick, and the helper-base shim would be shadowed.
`solve`/`solve_pairs` stay exactly as they are; nothing in this plan renames or reuses them.
`DispatchPolicy.decide` stops being `@abstractmethod` (`base.py:160`).

### 2.2 Wrapper interface (`policies/dispatch/bind.py`, new, WS1)

```python
class SolverWrapper(Protocol[P]):
    def bind(self, params: P, world: RunWorld, *, top: bool) -> Declaration:
        """Run start, once per node per process. MUST be pure in (params, world): no RNG,
        no mutation of params/world, no I/O. Default (DispatchPolicy): Declaration()."""
    def solve_tick(self, tick: Tick, params: P) -> Result:
        """One decision. `params` is authoritative (== the node's validated params).
        Must not mutate tick documents (base.py:58-67 contract, kept)."""
```

- Construction is unchanged from `profile.py:336,344`: `cls(params=<validated dict>)` for a
  leaf, and `cls(params=..., inner=<built child>, inner_node=<child ResolvedDispatch>)` for a
  node with an `inner` ref. A class that rejects those kwargs raises `PolicyError` (this replaces
  the `issubclass(CompositePolicy)` check at `profile.py:338-341`).
- `DispatchPolicy` (`base.py`) becomes the optional helper base. It provides the injection API
  (`set_rng`/`set_tiebreak`/`set_rebate_book`, `:80-139`) and `decision_rng` (`:141-158`). It also
  provides a default `bind` that returns `Declaration()`, and a default `solve_tick(tick, params)`
  for classes that only define `decide(ctx)`. That default asserts `params is tick.params`, wraps
  the tick in `compat.legacy_context.LegacyDecideContext(tick)` and calls `self.decide(that_ctx)`.
  The return value may be a `Result`, or the old `Plan`: that class moves verbatim to
  `compat/legacy_context.py`, `dispatch/plan.py` re-exports it until WS8, and a `Plan` is
  converted to a `Result`. A chain longer than one is a `DispatchContractViolation`, because
  tails are not a framework concept (decision 2).
- **What the adapter serves (H1, scoped honestly).** `compat/legacy_context.py` maps the
  LEAF-style members that test and third-party `decide` policies use onto `Tick` +
  `solver_lib.candidates`:
  - `raw_trucks`, `raw_orders`, `trucks`, `orders`, `params`, `rng`, `tiebreak_seed`, `rebates`,
    `clock`, `id_index`: passed through;
  - `allowed`/`allowed_raw` → `permitted`; `accounting_cost` → `measure.cost`; `tick_seed` →
    `seed`; `deadhead_km`/`dual_cycle_cost` → `solver_lib.objective`;
  - `candidate_pairs(kind=None)` / `sampled_trucks()` → `solver_lib.candidates` with today's
    per-context memo semantics; `default_candidate_kind` = `params.candidates` if declared, else
    `"sampled"`; `candidate_settings`;
  - `scope` → a legacy `Scope(kind, key)`, where kind is `"tick"` at depth 0, else the key's
    prefix (`partition`/`company`).

  It does **not** serve the composite-internal members `ctx.world` (9 uses: `.rule`,
  `.structure_allows`, `.accounting_cost`, `.own_allowed`, `.max_travel_time_pickup`) or the
  scope builders `scopes.partition_context`/`company_context`, which are deleted. Their test
  composites are ported explicitly in WS1, and those test files are listed in §7.7.
- Built-ins implement `solve_tick` directly and never use the adapter. The legacy default decision
  of `PairSolver` (`pair_solver.py:58-79`) is rewritten as `solver_lib.pair_solver.PairSolver.solve_tick`:
  `kind = params.candidates if declared else "sampled"`; spatial →
  `solve_pairs(candidates.spatial_pairs(tick, s), cost=tick.measure.cost)`; else
  `solve(candidates.sampled_trucks(tick, s), list(tick.raw_orders), pair_allowed=tick.permitted, cost=tick.measure.cost)`,
  under `decision_rng`. The unregistered-instance kind comes from `compat/unregistered.py`.
- `CompositePolicy` (`composite.py`) stays as an optional helper (it holds `inner`/`inner_node`
  and forwards injections). The framework never `isinstance`-checks it (G6).

### 2.3 `Declaration` (`policies/dispatch/bind.py`, WS1; fields consumed in the WS noted)

```python
@dataclass(frozen=True)
class Declaration:
    cross_company: frozenset[tuple[str, str]] = frozenset()  # (owner_haulier, carrier_haulier). TOP only;
        # today: the active arm's adjacency, world.py:94-99 / allowed.py:58 (owner ∈ adjacency[carrier]). WS1
    candidate_hint: str | None = None      # read by a CALLER via tick.callee(ref) (H1/G1).
        # today: ResolvedDispatch.candidate_kind (resolved.py:30-32) read at compose.py:182,
        # pooled_planner.py:296,446. Pair leaves: "spatial" iff params.candidates == "spatial". WS1
    provenance: Mapping[str, Any] = {}     # JSON; stamped meta.policies.planner.provenance[<node path>].
        # today: candidate_settings + sample_scope (sim main :161-173, :184-194), information_model. WS4b
    stamp_settings: Mapping[str, Any] = {} # TOP only; merged into orsim_settings (e.g. "COOPERATION").
        # today: sim main :403-428 + pooled_planner.effective_cooperation_stamp :109-137. WS4b
    # collision rule (L4): a stamp_settings key already present in orsim_settings when the stamp is
    # merged (POLICIES, STREAM_GEO, FACILITY_RULES, ...) is refused with PolicyError (exit 2).
    run_name_segments: tuple[str, ...] = ()  # TOP only; appended after the legacy topology segment,
        # idempotent by segment. today: sim main :430-450 (default-arm path). WS4b
    run_stats: type | None = None          # TOP only; a RunStatsAggregator CLASS (a factory, so the
        # declaration stays frozen/comparable across the two processes). today: app.py:274-442. WS4a
```

`candidate_hint` sits on the `Declaration` rather than in registry metadata. That deviates from
the wording of decision (e), on purpose, because the hint depends on params (a GreedyNearest
inner is `spatial` or `sampled` by its `candidates` param) (L5).

Framework rules at bind (WS1; messages are new, numbered after the existing 22):

- `top.cross_company` holds `str` pairs with `owner != carrier`, and is a subset of the ceiling
  `constraints.declared_partnerships(profile)`. The ceiling is the union over
  `profile["cooperation"]["structures"][*]` of
  `{(owner, carrier) : owner ∈ share_adjacency(s)[carrier], owner != carrier}`. It is a new
  ~12-line function in the world layer `assignment/constraints.py`, next to `share_adjacency`
  (`:344-351`). Violation → **message 23** (`PolicyError`, fails the run before spawn).
- A non-top node: `cross_company ⊆ top.cross_company`, and `stamp_settings`, `run_name_segments`
  and `run_stats` must be empty. Violation → **message 24**.
- Built-in pair leaves receive `top=False` when nested and then declare no `cross_company` (H3).

### 2.4 `RunWorld` (`bind.py`, WS1)

```python
class RunWorld(Mapping[str, Any]):
    """Read-only, read-tracking view of the assignment profile minus selection keys.
    Excluded: "policies", "policies_format", "policy_capture" (framework selection) and
    compat.selection_keys.LEGACY_SELECTION_KEYS = ("strategy", "solver_params", "planner",
    "use_spatial_matching"). Values are the profile's own objects (read-only by contract)."""
    @classmethod
    def from_profile(cls, profile: Mapping[str, Any]) -> "RunWorld": ...
    reads: frozenset[str]        # every key accessed via [], get, in, iteration (iteration = all keys)
```

It replaces `DispatchWorld` (`world.py:72-180`; `structure`, `rule`, `component_of`,
`accounting_params` and `max_travel_time_pickup` all leave it) and `ctx.candidate_settings`
(`context.py:122`). Cooperation reaches a solver only as `world.get("cooperation")`. Framework
code reads the profile directly (for `max_travel_time_pickup` and the ceiling), so its own reads
are never counted.

Read rules (M4):

- **A `bind` reads every world key its `solve_tick` will read.** For the built-ins that is:
  - pair leaves (top or nested): `spatial_cell_deg`, `spatial_candidates_per_order`,
    `spatial_max_rings`, `max_trucks_per_haulier`, plus `cooperation` at top;
  - Partitioned: `cooperation`, plus its inner's keys through the inner's own bind;
  - PooledMarket: `cooperation`, `max_travel_time_pickup` (the moved `CompanyRule`), and the four
    candidate keys (the round prebuild).

  The WS4b battery check `contract.world_reads_at_bind` enforces this on every fixture.
- `BoundNode.world_reads` is a frozen snapshot taken when that node's `bind` returns. Reads made
  during `solve_tick` are recorded separately, for the battery check only.
- The runner cache-key digest is
  `json.dumps({k: prof.get(k, _ABSENT) for k in sorted(tree.world_reads)}, sort_keys=True, default=str)`.
  The sentinel matters: a key read with `get`/`in` may be absent, and `prof[k]` would raise.
- Capture and the stamp serialise the RAW profile slice, never by iterating `RunWorld`.
  Iterating records every key as read, which would silently disable message 25.

### 2.5 `Tick` (`policies/dispatch/tick.py`, new, WS1; replaces `context.py` + `scopes.py`)

| Member | Meaning | Today (file:line) |
|---|---|---|
| `raw_trucks`, `raw_orders` | read-only documents (tuples) | `context.py:124-125,160-161` |
| `trucks`, `orders` | lazy typed views; `haulier_id` = `constraints.haulier_of` | `context.py:184-192`, `views.py:71-90` |
| `id_index` | `(trucks_by_id, orders_by_id)`, first doc per id | `context.py:194-203` |
| `clock` | `DispatchClock(sim_clock, time_step, horizon_steps)` (kept) | `context.py:93-99` |
| `seed` | `blake2b(run_id\|step)`, inherited by sub-ticks | `world.py:46-54`, `context.py:132,166` |
| `rng` | top: `Random(seed)`, built on first read; sub: the callee **instance's** injected rng (the random module by default), bound by `call` | `context.py:177-182`; `scopes.py:187,232` |
| `tiebreak_seed` | top: `seed`; sub: the callee instance's | `scopes.py:120,188,233` |
| `sample_rng` | top: `rng`; sub: what the caller passed to `restrict` (required kwarg; `None` = sub's `rng`) | `context.py:129,173,222`; `scopes.py:197,242` |
| `rebates` | top: top instance's `rebate_book`; sub: callee's | `scopes.py:121,189,234` |
| `world` | `RunWorld` (shared by every sub-tick) | `world.py`, `context.py:122,165` |
| `scope` | free-form `str`: `"tick"` at top, the caller's key below | `Scope` `context.py:79-90` |
| `depth` | 0 at top, +1 per `restrict` | replaces `scope.kind == "tick"` (`context.py:180,218`) |
| `candidates` | caller-supplied candidate pairs, or `None` (not supplied); a hint, not validated | `scopes.py:217,227,241` (`prebuilt_pairs`) |
| `scratch` | per-tick-object dict for solver helpers (memos); the framework never reads it | `context.py:174-175` caches |
| `params` | the node's validated params (for the legacy `decide` shim only) | `context.py:117,153` |
| `permitted(t, o)` | top: `owns_or_declared(t,o) and physical(t,o)`. sub: `parent.permitted and narrow`. Accepts views or docs. | `allowed_raw`/`allowed` `context.py:126,205-207`; `world.tick_allowed` `world.py:132-155`; `PartitionRule.allowed` `allowed.py:48-62` |
| `serves(carrier_id, owner_id)` | `carrier == owner or (owner, carrier) in top.cross_company` | `PartitionRule.serves` `allowed.py:64-68`; `world.may_serve` `world.py:169-171` |
| `measure` | `Measure`: `deadhead_km(t,o)`, `cost(t,o)` = `constraints.assignment_cost(t, o, dials)`. Dials (M5): **live** = `AssignmentApp._solver_params` exactly as today (set at init `app.py:70`, set explicitly by the frozen harness `harness.py:107`, passed in by the app); **offline** (battery/fixtures/bench/capture replay) = `compat.measurement.accounting_params_for(tree)` (first node wins, `runner.py:255-273`) unless the fixture sets `accounting_params` | `context.py:262-270`; `world.accounting_cost` `world.py:161-163`; `runner.framework_accounting_cost` `:276-290` |
| `execute(fn, *a)` | offload hook | `context.py:74-76`, `app.py:43-45,538` |
| `restrict(*, trucks, orders, scope, sample_rng, candidates=None, narrow=None)` | sub-tick. Raises `DispatchContractViolation` if any truck/order is not (by `id()`) in this tick. Records a scope_log entry `{key, depth, trucks, orders}` when auditing | `scopes.partition_context :141-200`, `company_context :203-245`, `_log_scope :70-80` |
| `call(ref, sub) -> Result \| None` | nested solve under the same contract (§2.7) | `compose.decide_in_scope` `compose.py:121-147`, `ScopeDecider.run` `compose.py:201-211` |
| `callee(ref) -> BoundNode` | the bound child for this node's ref (name, version, params, declaration) | `ScopeDecider.candidate_kind` `compose.py:182` |
| `docs(result) -> list[(truck_doc, order_doc)]` | a result's pairs as this tick's documents (first doc per id) | `compose.heads_to_pairs` `compose.py:102-105` |
| `note_incident()` | counts a swallowed solver error (the caller logs it) | `stats["crashes"]` `compose.py:213-215`, read at `app.py:540-544` |

These are deliberately absent: `owner_of`, any company/haulier map, the cooperation
rule/structure/components/pools, `candidate_pairs`, `sampled_trucks`, `default_candidate_kind`,
`candidate_settings`, `rule` (`context.py:130,164`) and `verified_heads` as a public member.
Candidate generation becomes solver helpers in `solver_lib/candidates.py` that take a `Tick`.
The framework keeps `stats`, `scope_log`, `capture` and the verified-pair set as private
attributes.

**Minimal author surface** (documented as "the contract"; everything else is "advanced"):
5 registry fields (slot, name, version, description, `Params`), `solve_tick(tick, params)`,
`tick.raw_trucks`/`raw_orders` (or the views), `tick.permitted`, `tick.measure`, `tick.rng`,
and `Result.pairs`. Plus the battery's determinism declarations (G12).

### 2.6 `Result` (`policies/dispatch/result.py`, new, WS1; replaces `plan.py`)

```python
@dataclass(frozen=True)
class Result:
    pairs: tuple[tuple[str, str], ...]                                   # (truck_id, order_id), commit order
    solver_notes: Mapping[tuple[str, str], Mapping[str, Any]] = {}       # opaque, JSON values, keys ⊆ pairs
    tick_notes: Mapping[str, Any] = {}                                   # opaque; to the top's run_stats only

    @classmethod
    def from_pairs(cls, doc_pairs, *, solver_notes=None, tick_notes=None) -> "Result":
        """(truck_doc, order_doc) pairs in order. Raises DispatchContractViolation when a truck
        repeats (plan.py:48-66 rule, same message)."""
```

| Field | Today |
|---|---|
| `pairs` | `Plan.chains` / `Plan.heads()` (`plan.py:35,68-70`); every shipped chain has length 1 |
| `solver_notes` | `notes["pair_notes"]` (`pooled_market.py:165`, built at `pooled_planner.py:609-621`) |
| `tick_notes` | `notes["market"]` (`pooled_market.py:157-164`), read at `app.py:551-556` |

**Generic checks** (`invariants.check_result(result, tick) -> list[str]`, replacing `check_plan`
`invariants.py:30-57`). A problem is reported when: the result is not a `Result`; a truck id is
not in scope; an order id is not in scope; a truck appears twice; an order appears twice; a pair
is not `permitted`; or a `solver_notes` key is not one of the result's pairs, or its value is
not a `Mapping` with `str` keys. Message 18 (top, commit nothing) and message 19 (sub-tick, drop
that scope) keep their wording, with "plan" → "result".

**Verified-pair memo (H7).** `call` records the `(id(t), id(o))` of every pair that passed a
sub-tick's check. A sub-tick's `permitted` is always framework-composed and narrower than the
parent's. The top check therefore re-checks only `owns_or_declared` for those pairs and the full
`permitted` for everything else. This is today's shape (`world.tick_allowed`,
`compose._record_verified_heads` `compose.py:150-168`) without the `proves_physical` type list
(`world.py:38-43,121-130`).

### 2.7 How the framework runs a tick (identical for every solver)

Run start (sim main, run-start check, control dry run, and the agent; the same function
`bind.bind_profile(profile, instance=None) -> BoundTree`):

1. `profile.resolve_dispatch_for_profile` → `ResolvedDispatch` tree (single `inner`, `profile.py:250-278`).
2. `build_policy_tree` (instances); for the agent, `bind_instance` fits `AssignmentApp._solver` (`profile.py:435-491`).
3. Pre-order `bind(params, RunWorld, top=(node is root))` per node → `BoundNode(node, spec,
   instance, declaration, world_reads, inner, path)`, then the §2.3 checks (messages 23/24).
4. `BoundTree.world_reads` = the union of the nodes' reads. The override layer passes the world
   keys it patched (e.g. `{"cooperation"}` from `--structure`). If a patched key was not read,
   that is **message 25** (WS7).

Per tick (`AssignmentApp._dispatch_tick`, `app.py:506-558`):

1. Fetch and filter trucks/orders (`app.py:444-484`, unchanged).
2. `tick = runner.tick(...)`; `result = top.solve_tick(tick, params)`, offloaded through
   `tick.execute` iff `top.spec.offload == "self"` (`runner.py:174-189`).
3. `check_result` → message 18 / commit nothing.
4. `pairs = tick.docs(result)`; measurement: `account_shares(pairs, candidates, cost=measure.cost,
   own_allowed=constraints.pair_allowed(mtt))` → `render_share_tags(fmt=compat.wire.share_tag_format_for(top.name),
   solver_notes=result.solver_notes)`.
5. `top.declaration.run_stats` aggregator: `fold(result.tick_notes, clock)` + PATCH `run_config.meta.<meta_key>` (WS4a).
   On a message-18 discard nothing is folded, as today (a violation yields `notes={}`, so
   `_record_round_stats` never ran) (L3).
6. One generic INFO line; publish.

`tick.call(ref, sub)`:

1. Resolve `child = tree.child_for(node, ref)`. Refuse with `DispatchContractViolation` a `sub`
   that this tick's `restrict` did not produce, or one already used by a `call` (M7). Every
   `restrict` returns a fresh, unbound sub-tick; one sub-tick serves one call.
2. Bind `sub.params`/`rng`/`tiebreak_seed`/`rebates` from `child.instance`. `child.instance` IS
   the object at `parent_instance.inner` (`build_policy_tree` builds the child once and passes
   it as `inner=`, `profile.py:343-344`). A rebuilt tree must preserve that identity, or
   PooledMarket's `self.inner.set_rng` would inject into an object `call` never uses (L6;
   G-gold would catch it).
3. Capture `before_solve(sub)`.
4. `result = sub.execute(child.instance.solve_tick, sub, child.params)` iff `child.spec.offload == "self"`,
   else a direct call. A `DispatchContractViolation` raised inside is a problem; any other
   exception propagates.
5. Capture the result; `check_result(result, sub)`; message 19 + `stats["violations"] += 1` →
   return `None`; else record the verified pairs and return the result.

This is `decide_in_scope` (`compose.py:121-147`) generalised.

### 2.8 `RunStatsAggregator` (`policies/dispatch/run_stats.py`, new, WS4a)

```python
class RunStatsAggregator(Protocol):
    meta_key: str                                            # run_config.meta.<meta_key> (PooledMarket: "market")
    def fold(self, tick_notes: Mapping[str, Any], clock: DispatchClock) -> bool: ...  # True = push NOW (forced)
    def push_due(self, *, force: bool = False) -> bool: ...  # owns the cadence (today: ticks % 5, grew max)
    def mark_pushed(self) -> None: ...
    def snapshot(self) -> dict[str, Any]: ...
    @classmethod
    def empty_snapshot(cls) -> dict[str, Any]: ...
```

The framework holds one instance per `meta_key` on the app, created from the top declaration's
class on first use, and it survives runner rebuilds the way `_round_stats` does today. Each tick:
`forced = agg.fold(...)`; if `forced and agg.push_due(force=True)`: mark + PATCH; then
`if agg.push_due()`: mark + PATCH. This reproduces `app.py:414-418` followed by `:556`. On
`close`, a forced PATCH (`app.py:431-442`). The generic PATCH helper is `app.py:347-375`
verbatim with `meta.<meta_key>`. The aggregator owns the truncation ERROR text (`app.py:419-429`).

## 3. Decision 1: haulier channels kept and removed

Kept (one field, one reader): `constraints.haulier_of(doc)` (`constraints.py:222-230`);
`TruckView/OrderView.haulier_id` (`views.py:76,90`, the typed view of the same field); the
framework's `owns_or_declared` (reads `haulier_of` off both docs; "own" means both non-None and
equal, exactly `haulier_matches` `constraints.py:233-243`, H13); accounting
(`accounting.py:136-151,176-177`); the haulier-block warning (`app.py:492-503`).

Removed from the framework/tick (WS1):

| Duplicate channel | Where today |
|---|---|
| `DispatchWorld.component_of` (haulier → component index) | `world.py:57-69,85,100` |
| `DispatchWorld.partners(h)`, `may_serve(server, owner)`, `cooperation_pairs(hauliers)` (a haulier list "for NetworkFlow") | `world.py:165-180` |
| `scopes.company_context(haulier_id=)`, `company_scope_key(haulier_id)`, `partition_scope_key` | `scopes.py:49-57,203-245` |
| `ScopeDecider.company_rule(haulier_id, mtt)` | `compose.py:189-192` |
| `CompanyRule.haulier_id` as a framework type | `allowed.py:79-107` (moves into `pooled_market.py`, solver side) |
| `TICK_SAMPLE_SCOPE = "per_haulier"` and per-haulier sampling inside the context | `context.py:70-71,216-224` |
| `capture` recording `rule.to_dict()` adjacency | `capture.py:214-225` |
| the draft plan's proposed `owner_of(doc)` | not added |

Solver-internal haulier grouping stays where it is: PooledMarket `_by_haulier`
(`pooled_market.py:170-177`) and the market engine's `owner_by_order_id`
(`pooled_planner.py:343-361`) are a solver's own data structures.

## 4. Decision 2: pairs, and the check for tails

- Every shipped solver returns length-1 chains. GreedyNearest and RandomAssignment use
  `Plan.from_pairs` (`greedy_nearest.py:100-103`, `random_assignment.py:54-63`). PooledMarket uses
  `Plan.from_pairs(result.assignment)` (`pooled_market.py:167`). Partitioned merges its inner's
  chains (`partitioned.py:85-90`). The unregistered PairSolver path uses `from_pairs`
  (`pair_solver.py:69-72`).
- Nothing depends on tails:
  - capture records `live_heads` = `plan.heads()` (`capture.py:228,408`);
  - `policy_golden_v2` records heads;
  - `dispatch_golden_v1` records committed pairs and published bytes;
  - the runner commits heads only (`compose.py:102-105`).
- `slots.py:69` declares `timings=("online","plan")` with `executable_timings=("online",)`. The
  resolver already refuses `plan` (`resolve.py:67`, `runner.py:111-115`), so nothing executes it.
  **G13 → D in WS4b:** remove `"plan"` from the declared timings, because it names the
  held-tails concept.
- The only tests with tails are the unit tests in `tests/test_dispatch_plan_and_invariants.py`
  (`:113,124,135,145,206`). WS1 rewrites them to pairs. The ~20 test files that build
  `Plan(chains={t: (o,)})` become `Result(pairs=((t, o),))` (mechanical).
- Byte identity: `pairs` order = chain insertion order = commit order. A golden-v2 "heads" list is
  `[list(p) for p in result.pairs]`, the same JSON.

## 5. Audit list: final verdicts

Verdicts: **K** keep (generic), **M** move to the solver side, **D** delete, **Q** quarantine
in `policies/compat/`. WS = workstream (§7).

### 5.1 The draft plan's 34 items

| # | Item | Where (35b5b92) | Verdict | WS |
|---|---|---|---|---|
| 1 | `INFORMATION_MODELS`, required for planner | `registry.py:73-74,117,182,218,280-288` | **D**; optional `badge`/`badge_tooltip` display text + provenance | 4b |
| 2 | `information_model` in resolved/stamp/catalog/capture/CLI/bench/sim main | `resolved.py:43-57`; `resolve.py:79,91,237,608-622`; `catalog.py:74`; `capture.py:13,186`; `policy_cmd.py:54,186-188`; `bench.py:204,245`; sim main `:168,178` | **D** (FE reads old stamps, WS6) | 4b (capture: 1) |
| 3 | `sample_scope_for`, `TICK_SAMPLE_SCOPE` | sim main `:164-168,184-194`; `context.py:27-38,70-71` | **M**: `Declaration.provenance["sample_scope"]` (pair leaf at top `per_haulier`, Partitioned `per_group`, PooledMarket `per_company`; same values) | 1 (context), 4b (stamp) |
| 4 | `SHARE_TAG_FORMATS`, `V1_SHARE_TAG_BUILTINS` | `registry.py:77-83,291-301`; `resolved.py:57`; `runner.py:125`; `app.py:548`; `accounting.py:195-203` | **Q**: `compat/wire.py` `share_tag_format_for(top_name)` + `render_v1_tag`; the registry loses the kwarg (message 15 retired: no policy can opt in) | 3 (render), 4b (registry/stamp) |
| 5 | `CandidateSettings` as a framework type | `helpers/candidates.py:60-93`; `context.py:122,158`; `runner.py:129-132`; sim main `:162-170` | **M** to `solver_lib/candidates.py` (`from_world(world)`), stamped in provenance | 1, 4b |
| 6 | the framework reads `params.candidates` | `resolved.py:23-32`; `profile.py:273,363-373`; `runner.py:116-120,124` | **M**: leaves already read their own (`greedy_nearest.py:99`, `random_assignment.py:53`); hint via `Declaration.candidate_hint`; the unregistered-class rule (`profile.py:363-373`, `hasattr(solve_pairs)` + `use_spatial_matching`) **Q** `compat/unregistered.py` | 1 |
| 7 | framework-built candidates | `context.py:209-260`; `scopes.py:112-128,178-181,227-241` | **M**: `solver_lib/candidates.py` `spatial_pairs(tick, settings)` (eligibility from `tick.serves`, feasibility `tick.permitted`; verbatim body of `spatial_pairs_for_partition` `candidates.py:96-138`), `sampled_trucks(tick, settings)` (memo in `tick.scratch`, draws once), `full_pairs(tick)` | 1 |
| 8 | `allowed_raw` handed to the solver | `pair_solver.py:58-79`; `scopes.py:126,194,239` | **K** as `tick.permitted` | 1 |
| 9 | `DispatchWorld` cooperation members | `world.py:72-180` | **M**: arm resolution to `solver_lib/cooperation.py`; ownership to `tick.permitted/serves`; `DispatchWorld` deleted | 1 |
| 10 | `validate_cooperation_world` (message 14) | `world.py:183-206`; `profile.py:113,126` | **M** to `solver_lib/cooperation.validate_arm`, called from the built-ins' top `bind` (run start binds, so coverage is unchanged for every built-in); text unchanged | 1 |
| 11 | `PartitionRule`, `CompanyRule`, `_PHYSICAL_PROVING_RULES` | `allowed.py:33-107`; `world.py:38-43,121-130`; `compose.py:34,189-192` | **D** PartitionRule (replaced by permitted/serves, pinned by WS0 oracles); **M** CompanyRule to `pooled_market.py` (used only by `build_round_candidates`, `pooled_planner.py:319-321,435-443`); **D** proves_physical | 1 |
| 12 | `compose.cooperation_groups` | `compose.py:63-99` | **M** verbatim to `library/partitioned.py`, reading `component_of` from `solver_lib.cooperation.resolve_arm(tick.world)` | 1 |
| 13 | `Scope.kind` Literal, scope key builders | `context.py:79-90`; `scopes.py:49-57,141-245` | **K** `restrict(scope=str)`; key strings move into the solvers unchanged (`partition:component:N`, `partition:haulier:H`, `company:H`) | 1 |
| 14 | `ScopeDecider` | `compose.py:171-221` | **M** to `pooled_market.py` as its company decider over `tick.restrict/call/callee/docs/note_incident`; message 20 text unchanged, owned by the solver | 1 |
| 15 | hardcoded component modules | `components.py:62-70,143-164` | **M**: owners declare `register_component_kind(..., order=N)`; built-in kinds load via `registry.discover_builtin_policies()` (`pooled_market` → `pooled_planner.py:33-34` imports them) | 5 |
| 16 | `legacy.py` | `legacy.py:66-374` | **Q** `compat/legacy.py` (shim at the old path until WS8) | 2 |
| 17 | mirrors + `check_mirrors` (message 13) | `profile.py:494-638`; `spec_compile.py:20-21,75-103` | **Q** `compat/mirrors.py`; echoes still written (bundle bytes unchanged) | 2 |
| 18 | accounting dials "first node wins" | `runner.py:252-290`; `resolve.py:612-627`; `app.py:70,176` | **Q** `compat/measurement.py`; PooledMarket bid cost from its inner (G2, WS4a); world-setting move = later phase (§11) | 1 (move), 4a |
| 19 | market round stats | `app.py:7-11,274-442,551-556`; `runner.py:20-21`; `pooled_market.py:51-52,157-164` | **M** verbatim to `library/_market_stats.py` (`_` prefix: not a discovered policy module, `registry.py:468`) as a `RunStatsAggregator` (meta_key `market`); `compat/run_stats.py` keeps `AssignmentApp.round_stats()` (the frozen golden harness calls it, `tests/golden/harness.py:136`) | 4a |
| 20 | `pair_notes`, `PAIR_NOTE_KEYS`, wire fields | `accounting.py:47-49,227-253`; `app.py:549,592-598`; `message_data_models.py:22-30,49-52`; `truck/app.py:381-393` | **K** generic `solver_notes` + **Q** `compat/wire.py` note-name table; a new optional `solver_notes` payload field, omitted when `None` | 3 |
| 21 | `_log_cooperation` | `app.py:557-575` | **D**; one generic INFO line | 1 (H4) |
| 22 | `COOPERATION` stamp + default-path `· <arm>` | sim main `:403-459`; `pooled_planner.py:109-137`; consumer `analytics/manager.py:69-82,1254,1382` | **M**: `Declaration.stamp_settings["COOPERATION"]` = `solver_lib.cooperation.stamp(arm)` = `{structure_id, components, adjacency, pools}` (S5; `topology` and `market` dropped, since the market config is already in `meta.policies.planner.params`); `run_name_segments=(arm.id,)`; `effective_cooperation_stamp` and the now-unused `resolve_market_config` D | 4b |
| 23 | topology segment, `ORSIM_PLANNER_TOPOLOGY`, market env | sim main `:88-112,385-399`; `scenario_overrides.py:153-160,242-299,326-428,614-672` | **Q** `compat/legacy_env.py`; `run_dispatch_overrides(..., legacy_env=)` opaque | 2 |
| 24 | slot default `Partitioned`, description | `slots.py:59-71` | **K** the value; the constant moves to `compat/defaults.py` (`DEFAULT_PLANNER`, stdlib-only); description reworded generically | 2 |
| 25 | injection API, `decision_rng`, CompositePolicy forwarding | `base.py:70-158`; `composite.py:37-74`; `app.py:259-262` | **K** as the optional helper base; the sub-tick decision RNG is the callee instance's injection (H9) | 1 |
| 26 | battery composite/budget/model audits | `battery.py:101-131,269-306,927-1000,1075-1087` | **K** ref-based arms + budgets; **M** the scope audit → optional wrapper hook `audit_scopes(scope_log, tick) -> list[str]` (Partitioned/PooledMarket get `_scope_problems` logic); **D** `_model_declared` | 4b |
| 27 | capture records the active structure + model/format | `capture.py:13,186-187,214-225,335-344` | **M**: format `/3` records the `RunWorld` mapping + declaration; `/2` reader → `compat/capture_v2.py`; `live_heads` → `live_pairs` | 1 |
| 28 | fixtures carry cooperation | `testing/fixtures.py:79-131` | **K** test data, re-plumbed to `RunWorld`/`bind_profile` | 1 |
| 29 | refuse a composite not delegating via `inner` | `profile.py:250-278` | **K** (decision g: single inner) | – |
| 30 | FE information-model badge | fe `lib/policy-slots.ts:645-725`; `PolicySlotsEditor.tsx:41,70,152`; `policy-catalog-types.ts:61-84,141-149`; `compareRunMeta.ts:1,35,66,166` | **M** to catalog `badge`/`badge_tooltip`, falling back to `information_model` for old backends/runs | 6 |
| 31 | FE `KNOWN_TOPOLOGIES` | fe `lib/runNameMeta.ts:83-95` | **Q** (old run names exist): comment as compat, no logic change | 6 |
| 32 | FE `PolicyParamsForm` | fe | **K** | – |
| 33 | control plane, scenario CLI | `openride_control/policies.py`, `openride/scenario_cmd.py` | **K** (WS7 extends the dry run) | 7 |
| 34 | `planner.sharing` / `ORSIM_SHARING_POLICY` (inert at run time: only `datagen/preprocess.py:207` reads `planner.sharing`, at compile) | sim main `:369-385`; `scenario_overrides.py:554-611`; `openride/runner.py:86-142`; `run_group.py:146,212`; `run_cmd.py:102`; `openride_control/manager.py:462-504`; fe `app/api/scenario/run/route.ts:38-55,161` | **D** the run-time override; the sim main refuses a set `ORSIM_SHARING_POLICY` with a message (set `planner.sharing` in spec.json and recompile) | 7, 6 |

### 5.2 The critique's G1–G12

| # | Item | Where | Verdict | WS |
|---|---|---|---|---|
| G1 | the inner's `candidates` drives the market prebuild; the company scope serves the prebuilt pairs | `pooled_planner.py:296-314,446-458`; `compose.py:182`; `scopes.py:217-241`; `context.py:241-253` | **K** generic channel: callee `Declaration.candidate_hint`, read via `tick.callee(ref)`; `restrict(candidates=)`; the raise at `context.py:243-252` **D** (a leaf with `tick.candidates is None` builds its own) | 1 |
| G2 | the framework's accounting cost is the market's decision cost | `pooled_market.py:149`; `pooled_planner.py:312,380,545,563` | **M**: `PooledMarket` builds `objective.dual_cycle_cost_fn(bonus_km, radius_km)` from `tick.callee(p.inner).params` when declared, else the constraint defaults. Byte-identical on all 68 golden cases and all 17 bundles (all dials at defaults). Off-default boundary documented: pooled + cost-blind inner + non-default `solver_params` | 4a |
| G3 | sampling keyed on `scope.kind == "tick"` | `context.py:216-223` | **M** to `solver_lib.candidates.sampled_trucks`, keyed on `tick.depth == 0` (the pair leaf's stamped rule) | 1 |
| G4 | `stats["crashes"]` read by the app | `app.py:489-493,540-544`; `compose.py:213-221` | **K** generic `tick.note_incident()`; the app counts violations + incidents | 1 |
| G5 | registration requires `DispatchPolicy` | `registry.py:67-71,376-385` | **K** relaxed to "defines `solve_tick` (or legacy `decide`)"; not `solve`, which is the pair API (B1) | 1 |
| G6 | `isinstance(CompositePolicy)` | `runner.py:58,122,177-185`; `battery.py:705-710`; `composite.py:77-81`; `app.py:147-169`; `profile.py:338-352,405-406,464-468` | **D**: walk the framework's `BoundTree`; offload from the entry | 1 |
| G7 | duplicate `INFORMATION_MODELS` in the battery | `battery.py:113,125,131,929-969` | **D** | 4b |
| G8 | single-`inner` descent | `profile.py:259-267`; `resolved.py:60`; `profile.py:319-344` | **K**, documented limit | – |
| G9 | analytics planner-group schema | `analytics/manager.py:69-82,1254,1382` | **K**; documented as the `COOPERATION` stamp schema `{structure_id, components}` a solver emits via `stamp_settings` to get planner rows | 4b, 8 |
| G10 | the override path appends `· <structure>` itself (before the topology segment) | sim main `:368-381` | **K** in the world-plumbing layer (byte-identical position); the default-path segment comes from `run_name_segments` (after topology, idempotent) | 4b |
| G11 | discovery scans two packages only | `registry.py:62-65,432-470` | **K**, stated; litmus (b) drops a real file into `library/` | 8 |
| G12 | battery declarations an author must satisfy | `order_sensitive`/`module_rng`/`no_module_rng`/perf | **K**; the authoring guide lists the minimal set | 8 |

### 5.3 Items this plan adds (found while checking)

| # | Item | Where | Verdict | WS |
|---|---|---|---|---|
| G13 | the `plan` timing (held tails) | `slots.py:69`; `resolve.py:67`; `runner.py:76-78` | **D** (§4) | 4b |
| G14 | `rule` on the context, captured | `context.py:130,164`; `scopes.py:130,198,243` | **D** | 1 |
| G15 | `policies/dispatch/helpers/` (candidates, objective) sit in the framework dir but are solver helpers | `helpers/candidates.py`, `helpers/objective.py` | **M** to `policies/solver_lib/` | 1 |
| G16 | `pair_solver.py` (a solver adapter) in the framework dir | `dispatch/pair_solver.py` | **M** to `solver_lib/pair_solver.py`; `assignment/solver/base.py:12-21` alias updated | 1 |
| G17 | `apply_cooperation_override` fail-soft (unknown id `:580-588`; no block `:589-594`) | `scenario_overrides.py:554-611` | **fail-loud**, **message 26**; also returns the patched world keys | 7 |
| G18 | the run-start check does not bind | `profile.py:100-128` | add `bind_profile` (messages 23/24 fail before spawn) | 1 |
| G19 | reserved note keys `market`/`pair_notes` (doc 26 trap 13) | `runner.py:20-21`; `app.py:549-552` | **D**: `Result` has typed fields | 1 |
| G20 | `DispatchRunner.run` accepting a sub-scope context offline | `runner.py:23-25,196-200` | **D**: sub-ticks exist only through `tick.call` | 1 |
| G21 | `PooledMarket.components()` re-validates `self.params` | `pooled_market.py:118-129` | **K** (solver-internal) | – |
| G22 | `effective_cooperation_stamp` reads the legacy `planner.topology` echo (`pooled_planner.py:123`; H12's ordering trap) | `pooled_planner.py:109-137` | **D** with #22 (so echo removal can never mislabel a run) | 4b |

## 6. Module map (old → new)

| Old (35b5b92) | New | WS |
|---|---|---|
| `dispatch/context.py`, `dispatch/scopes.py` | `dispatch/tick.py` (`Tick`, `Measure`, `DispatchClock`, `direct_execute`) | 1 |
| `dispatch/plan.py` | `dispatch/result.py` (`Result`) | 1 |
| `dispatch/world.py` | deleted: `tick_seed` → `dispatch/tick.py`; ownership → `tick.py`; cooperation → `solver_lib/cooperation.py`; message 14 → there | 1 |
| `dispatch/allowed.py` | deleted; `CompanyRule` → `library/pooled_market.py` | 1 |
| `dispatch/compose.py` | deleted: `heads_to_pairs` → `Tick.docs`; `decide_in_scope` → `Tick.call`; `cooperation_groups` → `library/partitioned.py`; `ScopeDecider` → `library/pooled_market.py`; `report_scope_violation` → `tick.py` | 1 |
| `dispatch/helpers/{candidates,objective}.py` | `solver_lib/{candidates,objective}.py` | 1 |
| `dispatch/pair_solver.py` | `solver_lib/pair_solver.py` | 1 |
| – | `dispatch/bind.py` (`RunWorld`, `Declaration`, `BoundNode`, `BoundTree`, `bind_tree`, `bind_profile`) | 1 |
| – | `solver_lib/cooperation.py` (`Arm`, `resolve_arm(world)`, `validate_arm`, `stamp(arm)`), `solver_lib/pair_leaf.py` (`PairLeafMixin.bind`: top → arm pairs; nested → `candidate_hint`; provenance) | 1 (stamp: 4b) |
| – | `compat/{selection_keys,unregistered,capture_v2,measurement}.py` | 1 |
| `dispatch/legacy.py`; `profile.py:494-638`; `scenario_overrides.py` legacy env | `compat/legacy.py`, `compat/mirrors.py`, `compat/legacy_env.py`, `compat/defaults.py` (old paths re-export until WS8) | 2 |
| `accounting._v1_tag`, `PAIR_NOTE_KEYS` | `compat/wire.py` | 3 |
| `app.py` round stats | `dispatch/run_stats.py` (protocol + PATCH helper), `library/_market_stats.py`, `compat/run_stats.py` | 4a |
| `components.BUILTIN_COMPONENT_MODULES` | `register_component_kind(order=)` | 5 |

`policies/compat/__init__.py` is created by WS0 so that parallel workstreams add sibling files
without conflicting.

## 7. Workstreams

### 7.0 Common setup (every coder)

```bash
BASE=/home/user/openride_apps_policy_slots          # integration worktree, branch feat/policy-slots
WT=/home/user/wt/<ws>                                # your worktree
git -C $BASE worktree add $WT -b ws/<ws> <base-commit>   # base: 35b5b92 (WS0, WS2, WS3, WS5) or the merge commit named below
ln -s $BASE/venv $WT/venv                            # untracked; NEVER `git add -A` / `git add .`
PY=$WT/venv/bin/python
SP=/tmp/claude-1000/-home-user/f13570e2-81af-483b-8703-76ae77d0783f/scratchpad
mkdir -p $SP/ws/<ws>
```

Rules:

- Follow the idioms already in use: module docstrings that state responsibility and boundary,
  frozen dataclasses, `PolicyError` subclasses, verbatim moves that cite their source, and
  "verified to have teeth" notes on new pins (mutate, see it fail, restore).
- Never touch `/home/user/orsim`. Never use a bare `git stash`; commit WIP on your `ws/` branch.
- Commit messages end with `Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>`.
- Do not regenerate either golden file, `tests/golden/harness.py` or `tests/golden/compare.py`:
  they must not differ from `35b5b92` (`git diff --exit-code 35b5b92 -- tests/golden/...`, part of G-gold).
- No celery restart or live run is part of this plan. The branch is not live (doc 26 §7).

### 7.1 Gate commands (referenced as G-gold, G-oog, G-val, G-full, G-fe, G-perf, G-vocab)

```bash
cd $WT
# G-gold: 101 passed, nothing else, AND the golden inputs are byte-untouched (H5). The old wording
# "no commit after 277e67b" was false: 2a1ccf2 created policy_golden_v2.json.gz and 4f01034/3d7e84e
# touched compare.py. The rule is: no change versus 35b5b92. The orchestrator also runs this per merge.
$PY -m pytest -q -p no:cacheprovider tests/test_dispatch_golden_identity.py tests/test_dispatch_golden_via_init.py \
    tests/test_golden_published_compare.py tests/test_policy_golden_versions.py
git diff --exit-code 35b5b92 -- tests/golden/dispatch_golden_v1.json.gz tests/golden/policy_golden_v2.json.gz \
    tests/golden/harness.py tests/golden/compare.py            # MUST exit 0
# G-oog: 204/204 identical to the 35b5b92 baseline. Normally runcmp only, on the fixed profiles.
# WS2 and WS8 move or delete the env translation that built the |envpooled profiles, so they ALSO
# regenerate the profiles in $WT and require byte equality first (M2). mkprof2.py is c3rev/mkprof.py
# with a compat-first import; verified to reproduce prof.json byte for byte at 35b5b92:
#   $PY $SP/oog/mkprof2.py $SP/ws/<ws>/prof.json && cmp $SP/oog/prof.json $SP/ws/<ws>/prof.json
$PY $SP/c3rev/runcmp.py $SP/oog/prof.json $SP/ws/<ws>/oog.json
$PY -c "import json; a=json.load(open('$SP/oog/out_base_35b5b92.json')); b=json.load(open('$SP/ws/<ws>/oog.json')); d=[k for k in a if a[k]!=b.get(k)]; print(len(a), len(b), d[:5]); assert len(a)==len(b)==204 and not d"
# G-val: exit 0 + PASS from the CLI, AND the per-check id/status table diffed against the baseline (M8).
# A PASS->SKIP swap that keeps the total shows up in the diff.
for n in GreedyNearest RandomAssignment Partitioned PooledMarket; do
  $PY -m openride policy validate planner $n > $SP/ws/<ws>/val_$n.txt 2>&1; echo "$n exit=$? $(tail -1 $SP/ws/<ws>/val_$n.txt)"; done
$SP/oog/valtable.sh $WT $SP/ws/<ws>/valtable   # baseline in SP/oog/valtable_base: 47/0 skip, 47/2, 47/3, 187/8
for n in GreedyNearest RandomAssignment Partitioned PooledMarket; do
  diff $SP/oog/valtable_base/valtable_$n.tsv $SP/ws/<ws>/valtable/valtable_$n.tsv; done   # empty unless the WS declares a delta table
# G-full: no new failure, no lost test, no new skip (M8). --continue-on-collection-errors is REQUIRED.
$PY -m pytest -q -p no:cacheprovider -rfEs --continue-on-collection-errors > $SP/ws/<ws>/full.log 2>&1; tail -1 $SP/ws/<ws>/full.log
grep -E '^(FAILED|ERROR) ' $SP/ws/<ws>/full.log | sed -E 's/ - .*$//' | sort -u > $SP/ws/<ws>/fail.txt
comm -13 $SP/oog/base_fail_35b5b92.txt $SP/ws/<ws>/fail.txt     # MUST print nothing
grep -E '^SKIPPED' $SP/ws/<ws>/full.log | sort > $SP/ws/<ws>/skipped.txt
diff $SP/oog/base_skipped_35b5b92.txt $SP/ws/<ws>/skipped.txt   # empty, or each new line justified in the hand-back
#   passed count (tail line) >= 2741 - <deleted tests, listed by node id in the hand-back> + <new tests>
# G-vocab (from WS0 on): vocabulary + import rules
$PY -m pytest -q -p no:cacheprovider tests/test_framework_vocabulary.py tests/test_framework_imports.py
# G-perf (WS1, WS4a): run by the ORCHESTRATOR, serially, on a quiet box with no G-full or other WS running
# (CLAUDE.md §17/§24). Old = $SP/wt_base (detached 35b5b92, made in WS0), new = $WT; private output dir (M3).
$SP/oog/perfdrive_ws.sh $WT $SP/ws/<ws>/perf 12   # prints old/new/ratio per golden case; load in load.txt
# G-fe (WS6): in the FE worktree
npx tsc --noEmit; echo tsc=$?          # 0
npx vitest run 2>&1 | tail -4          # >= 601 passed, 0 failed
```

### 7.2 Phases and merge order

```
Phase 0  WS0 pins + vocabulary guard + baselines                       (serial, small)        merge #1
Phase 1  WS2 compat move | WS5 component order | WS3 wire channel      (parallel, base 35b5b92) merge #2,#3,#4 as they pass
         WS1 core contract                                             (parallel, base 35b5b92; the long pole) rebase onto #4, merge #5
         WS6 FE                                                        (parallel from Phase 1; merged LAST)
Phase 2  WS4a run stats + bid cost | WS4b metadata/stamp | WS7 coop override + preflight
         DEVELOPED in parallel, all branched from merge #5; MERGED sequentially WS4a #6 -> WS4b #7 -> WS7 #8.
         Before its merge each rebases onto the previous merge and re-runs every gate (M10).
Phase 3  WS8 finish: shims out, guard to final, litmus, docs           (base = merge #8)      merge #9
         WS6 FE merged into FE feat/policy-slots after #9 (deploy order: backend first, doc 26 trap 11)
```

The orchestrator does the merges in `$BASE` with `git merge --no-ff ws/<ws>` and runs
G-gold + G-oog + G-full on the merge commit. Before handing back, a coder whose base is older than
the current `feat/policy-slots` rebases (`git rebase feat/policy-slots`), resolves conflicts and
re-runs every gate.

Expected conflicts, all trivial:

- `dispatch/profile.py` import block (WS2 vs WS1);
- `assignment/app.py:549` (WS3 renames one keyword inside a block that WS1 rewrites; take WS1's
  block with `solver_notes=`);
- the `tests/vocabulary_baseline/*.txt` files of shared modules (take the smaller set and
  re-run G-vocab);
- `library/pooled_market.py` `bind` (WS4a adds `run_stats=`, WS4b adds the stamp/provenance
  lines);
- sim main (WS4b `:140-194,:403-459`; WS7 `:365-383`).

### 7.3 WS0 — pins, vocabulary guard, baselines (base 35b5b92)

**Goal.** Lock what must not change, before anything moves, and start the guard.

Files (all new unless noted):

- `tests/test_ownership_oracle_pins.py`. Oracles are written in the test and never imported from
  the code under test. The code under test is reached through ONE helper,
  `_sut_permitted(profile) -> Callable[[t, o], bool]` (today:
  `DispatchWorld.from_profile(p, accounting_params={}).allowed`). WS1 swaps only this helper and
  `_sut_serves`. Pins:
  1. Over every golden case world (68) x tick state A/B/C/T (sampled 1/4 trucks x 1/3 orders, as
     `test_policy_round2_c3_review_fixes.py:100-123`), plus every `fixtures.synthetic_fixtures()`
     world and a haulier-less stray truck:
     `permitted ≡ physical ∧ (own(t,o) ∨ (haulier(o), haulier(t)) ∈ edges(active))`, where
     `edges(active) = {(owner, carrier): owner ∈ adjacency[carrier]}` and `own` = both non-None
     and equal. Assert at least one allowed cross pair on a coop case (not vacuous).
  2. `serves(c, o) ≡ c == o or (o, c) ∈ edges(active)` against `PartitionRule.serves`, over all
     haulier pairs of each world.
  3. `cooperation_groups` order: for each golden case/tick, the list of group keys equals an
     oracle "first-seen order of the orders' groups, groups with no trucks dropped"
     (`compose.py:63-99`). SUT helper `_sut_groups`; WS1 points it at `library.partitioned`.
  4. Ceiling: for every golden case and every main-tree bundle,
     `edges(active) ⊆ union over arms of edges(arm)`.
  5. Message-14 premise: every golden case and bundle has pools ⊆ adjacency (both directions).
     This is what makes PooledMarket's `narrow=None` byte-identical in WS1.
- `tests/test_framework_vocabulary.py` + `tests/vocabulary_baseline/<dotted.module>.txt`, one
  file per guarded module, sorted `token<TAB>count` lines.
  - The scan uses `ast`. It examines identifiers (`Name.id`, `Attribute.attr`, def/class names,
    args, keyword names, import aliases) and non-docstring `str` constants (f-string parts
    included). Comments and docstrings are skipped.
  - Tier A (core runtime): `policies/framework/*.py`, `policies/dispatch/*.py` (top level only;
    not `library/`), `policies/testing/battery.py`, `policies/testing/bench.py`,
    `assignment/app.py`, `assignment/accounting.py`.
  - Tier B (world plumbing): `scenario/scenario_overrides.py`,
    `apps/simulation/run_container_logistics_simulation.py`, `openride_control/policies.py`,
    `openride/policy_cmd.py`.
  - **Tokenising (H2).** Every identifier and every non-docstring string is split on `_`,
    camel-case boundaries (`PooledMarket` → `pooled`, `market`) and non-word characters, then
    lower-cased. A hit is any banned SUBWORD, or any banned whole identifier.
    - Banned subwords, both tiers: `pooled`, `market`, `partitioned`, `greedy`, `topology`,
      `pooledmarket`, `greedynearest`, `randomassignment`.
    - Tier A also bans: `cooperation`, `partners`, `component`+`of` as a pair (i.e. `component_of`).
    - `pool` is NOT a banned subword (it would hit `ThreadPoolExecutor`/`_eventlet_tpool`), and
      `partition` is not (it would hit `str.partition`). Their real cases are whole identifiers below.
  - Banned whole identifiers, both tiers: `pool_id market_round PartitionRule CompanyRule
    ScopeDecider INFORMATION_MODELS information_model per_group per_company SHARE_TAG_FORMATS
    share_tag_format V1_SHARE_TAG_BUILTINS PAIR_NOTE_KEYS round_stats candidate_kind
    candidate_kind_for KIND_WITHOUT_CANDIDATES_PARAM CANDIDATE_KINDS TICK_SAMPLE_SCOPE
    sample_scope_for wrap_legacy_leaf BUILTIN_COMPONENT_MODULES accounting_params_for
    _ACCOUNTING_KEYS dual_cycle_bonus_km dual_cycle_radius_km pools_from_structure PoolMarket
    structure_allows may_serve cooperation_pairs proves_physical tick_allowed _v1_tag
    partition_context company_context partition_scope_key company_scope_key`. Tier A also bans
    `active_cooperation_structure share_adjacency share_eligible`, and the exact string `"v1"`.
  - **Import rule (H2, M11)**, a separate test `tests/test_framework_imports.py` with its own
    per-module baseline:
    - Tier A may not import (by any spelling: `import`, `from`, `as`, or a local import inside a
      function) `policies.solver_lib`, `policies.dispatch.library`, `assignment.solver`,
      `assignment.pooled_planner`, `assignment.pools`, `assignment.policy`,
      `assignment.arbitration`.
    - From `policies.compat` it may import only an allow-listed set of `(module, symbol)` pairs,
      kept in the test. The end state is `compat.legacy:{check_policies_format, LEGACY_LEAF_DEFAULT}`,
      `compat.mirrors:{check_mirrors}`, `compat.measurement:{accounting_params_for,
      framework_accounting_cost}`, `compat.selection_keys:{LEGACY_SELECTION_KEYS}`,
      `compat.unregistered:{UnregisteredPairLeaf, legacy_spatial_rule}`,
      `compat.capture_v2:{read_fixture_v2}`, `compat.wire:{share_tag_format_for, stamp_fields,
      split_notes, render_v1_tag, render_audited_tag, wire_fields}`,
      `compat.run_stats:{LegacyAppAccessors}`, `compat.legacy_context:{LegacyDecideContext, Plan}`,
      `compat.defaults:{DEFAULT_PLANNER}`. A new symbol needs a reviewed allow-list edit.
    - From `assignment.constraints`, tier A may call only the allow-listed functions (the
      relabelling hole of H2): `haulier_of haulier_matches pair_allowed pair_allowed_ignoring_haulier
      assignment_cost filter_assignable_trucks declared_partnerships`, plus the geometry readers
      `views.py` and `Measure` use (`truck_anchor_loc order_pickup_loc order_dropoff_loc
      _truck_last_dropoff_loc _truck_last_dropoff_facility _haversine_km size_compatible
      _DEFAULT_DUAL_CYCLE_RADIUS_KM`). WS0 enumerates today's calls (verified today: also
      `active_cooperation_structure`, `share_adjacency`, `share_eligible`). Those three go into the
      baseline, and WS1 removes them. `declared_partnerships` is the ONLY cooperation-schema read
      the framework keeps (decision a).
  - Exempt entirely: `policies/compat/**`. Not guarded: `library/`, `solver_lib/`,
    `assignment/solver/`, the market modules, `constraints.py`, `fixtures.py`, datagen, scenario
    compile, the CLI scenario wizard, the FE.
  - The test fails on any hit not in the module's baseline file, and on a baseline line whose
    count is higher than the actual count, so baselines can only shrink (the test prints the
    command to shrink them).
  - **Transitional additions (H4).** A workstream may ADD a baseline line only when it carries a
    `# removed-by: WS<n>` tag naming a later workstream. The guard fails if a tagged line is still
    present after the named WS merges: each WS's gate checks that no line tagged with its own id
    remains. WS8 requires zero lines. WS0 runs the scan at HEAD and writes the baselines. It must review
    them: any hit that is not real solver vocabulary means the list is wrong. Tune the lists so
    the §5 end state is reachable with empty baselines (e.g. `str.partition` must not hit; that
    is why `partition` is not a banned identifier).
- `apps/container_logistics/policies/compat/__init__.py` (modified: new). Its docstring says this
  is the quarantine for old inputs and wire formats, the only place framework vocabulary may name
  solvers. Imports nothing.
- `tests/test_policy_import_cycles.py` (modified): add `apps.container_logistics.policies.compat`
  to `_LEAVES`.
- Baselines, already captured by this planner (re-check, do not regenerate):
  `SP/oog/{prof.json,out_base_35b5b92.json,base_fail_35b5b92.txt,base_full_35b5b92.log,val_base_*.txt}`.
  `SP/oog/{valtable_base/,base_skipped_35b5b92.txt,stamp_base_35b5b92.json}` and the scripts
  `SP/oog/{mkprof2.py,valtable.sh,perfdrive_ws.sh,stamp_harness2.py}` were added in the revision
  (they run at 35b5b92; outputs re-measured).
  Create the perf baseline worktree: `git -C $BASE worktree add --detach $SP/wt_base 35b5b92 && ln -s $BASE/venv $SP/wt_base/venv`.

Gates: G-gold, G-full, G-vocab; the new pins pass. Teeth (record in docstrings):

- drop the `share_eligible` branch from `PartitionRule.allowed` (`allowed.py:58-61`) → pin 1
  fails. NOT `structure_allows`: `DispatchWorld.allowed` delegates to `self.rule.allowed`
  (`world.py:105-107`), so mutating `structure_allows` leaves pin 1 green (M1);
- reverse `cooperation_groups` → pin 3 fails;
- add `if top.name == "PooledMarket":` to `app.py` → G-vocab fails (camel-case subword);
- add `market_notes = 1` to `app.py` → fails; add `from apps.container_logistics.policies.solver_lib import x`
  inside a function in `runner.py` → `test_framework_imports` fails;
- lower a baseline count → it fails.

Done when: merged, all gates green, baselines reviewed (list the per-module hit counts in the
hand-back).

### 7.4 WS2 — compat quarantine of the legacy translation (base 35b5b92; no behaviour change)

Files:

- `policies/compat/legacy.py`: verbatim move of `dispatch/legacy.py:1-374`. It adds
  `LEGACY_SELECTION_KEYS` only if WS1 has not; prefer leaving that to WS1's
  `compat/selection_keys.py`.
- `dispatch/legacy.py`: a shim, `from apps.container_logistics.policies.compat.legacy import *`
  plus the explicit `__all__`. It is removed in WS8.
- `policies/compat/mirrors.py`: verbatim `profile.py:494-638` (`_first_declared`,
  `mirror_dispatch_selection`, `check_mirrors`, `_check_*`, `_same_value`, `_MARKET_KEYS`).
  `profile.py` re-imports them from compat (removed in WS8), keeping
  `profile.check_mirrors`/`mirror_dispatch_selection` importable.
- `policies/compat/legacy_env.py`: move `scenario_overrides.py` `MARKET_ENV :153-160`,
  `legacy_topology_name :242-261`, `_topology_top :263-270`, `_market_request :272-298`,
  `_refuse_market_conflict :386-399`, `known_planner_topologies :614-626`,
  `apply_planner_topology_override :628-645`, `apply_market_override :647-672`, and the
  topology/market branch logic of `_dispatch_entry :326-366`. Add
  `read_legacy_dispatch_env(environ) -> dict[str, str | None]` (sim main `:98-102`).
  - `run_dispatch_overrides(collection, *, overrides=None, solver=None, legacy_env=None)`.
    `legacy_env` is opaque to scenario_overrides and handed to compat. Its labels become
    `{"planner": <label|None>, "legacy": [<segments>]}`.
  - Sim main `:88-112` passes `legacy_env=compat.legacy_env.read_legacy_dispatch_env(os.environ)`.
    Sim main `:385-399` appends `labels["legacy"]` in the same position, so names stay
    byte-identical.
  - `scenario_overrides` re-exports the moved public names until WS8.
- `policies/compat/defaults.py` (stdlib only): `DEFAULT_PLANNER = "Partitioned"`.
  `framework/slots.py:66` uses it; the description `:67-68` is reworded generically ("which free
  truck serves which unassigned order, decided for the whole tick by the selected solver").
- Importers switched to compat directly: `scenario/spec_compile.py:20`,
  `datagen/preprocess.py:42`, `scenario/frontend_scenario_spec.py`,
  `assignment/solver/__init__.py:14`.
- Tests: `test_legacy_run_env_overrides.py`, `test_scenario_market_overrides.py` and any test
  calling `run_dispatch_overrides(..., topology=/offer=/...)` move to `legacy_env=`.
  `test_policy_import_cycles.py` `_LEAVES` adds `compat.legacy` and `compat.defaults`.
  Vocabulary baselines shrink.

Gates: G-gold, G-oog, G-val (counts unchanged), G-full, G-vocab. Also `scenario compile` on 3
scenarios into a temp dir; the agents must be identical to the main tree's compile. Use the
existing `test_policies_spec_carry.py` /
`test_frontend_scenario_generate.py`, which are in G-full.

Done when: behaviour, bundle bytes and run names are identical (G-oog checks the env-pooled
path), no legacy-solver vocabulary is left in `scenario_overrides.py`/sim main other than
`legacy_env` plumbing, and the baselines of those files are empty except re-export lines.

### 7.5 WS5 — component kinds declare their own order (base 35b5b92)

Files:

- `framework/components.py`: delete `BUILTIN_COMPONENT_MODULES :62-70`.
  `register_component_kind(kind, getter, default, description, *, order: int = 1000)`;
  `ComponentKind.order`. `_load_builtin_kinds :143-157` calls
  `registry.discover_builtin_policies()` (a lazy local import). `_order :160-164` becomes
  `(spec.order, _KIND_SEQ[kind])`.
- `assignment/policy/offer.py:255` `order=10`; `assignment/policy/claim.py:254` `order=20`;
  `assignment/arbitration.py:324` `order=30`.
- Tests: `tests/test_policy_components.py:172` (unchanged assertion);
  `tests/test_policy_framework_review_fixes.py:249-281` (cold-start probe: assertions unchanged,
  docstring teeth updated); a new test that an `order=` kind sorts before a default one.

Risk to check: a component-ref lookup during discovery returns early (`_DISCOVERY["running"]`).
Built-ins are safe because `pooled_planner.py:33-34` imports the component modules before the
`@policy` decorator on `PooledMarket` runs. Add a test asserting `PooledMarket` registers in a
cold interpreter.

Gates: G-gold, G-val (PooledMarket 179+8), G-full, G-vocab (`components.py` baseline → empty).
Done when: `framework/components.py` names no market module, and `openride policy list` output
is unchanged (diff it against the base).

### 7.6 WS3 — the wire channel `solver_notes` (base 35b5b92)

Files:

- `policies/compat/wire.py` holds every wire-format detail that names old solver provenance (H3):
  - `LEGACY_NOTE_FIELDS = ("pool_id", "market_round")` and `split_notes(note) -> (legacy, rest)`.
  - `render_v1_tag(acc, rest)`: verbatim `accounting._v1_tag :195-203`, plus `"solver_notes": rest`
    appended only when `rest` is non-empty (H6). Byte-identical: no built-in leaf emits notes.
  - `render_audited_tag(acc, legacy, rest) -> dict`: verbatim `_audited_tag :206-224`, the same
    8 keys in the same order (`pool_id` 5th, `market_round` 8th), plus `"solver_notes": rest`
    appended last only when non-empty.
  - `wire_fields(tag) -> dict`: the keyword set `publish` passes today
    (`shared, owner_haulier_id, carrier_haulier_id, benefit_km, pool_id, awarded_cost_km,
    owner_reserve_km, market_round`; `app.py:588-598`), each `tag.get(k)` with today's defaults
    (`shared` → `bool(tag.get("shared", False))`, the rest `None`), plus `solver_notes` when the tag has it.
- `assignment/accounting.py` keeps `ShareAccount`, `owner_reserve_km`, `finite_or_none` and
  `account_shares`. `render_share_tags(accounts, *, fmt, solver_notes=None)` becomes a dispatcher
  over the two compat renderers, so `accounting.py` no longer contains `pool_id`, `market_round`,
  `_v1_tag` or `"v1"`. Delete `PAIR_NOTE_KEYS :47-49`.
- `message_data_models.py`: `solver_notes: Optional[Dict[str, Any]] = None`, appended LAST;
  `parse` reads `payload.get("solver_notes")`; `to_wire_dict()` returns `dict(self.__dict__)`
  minus `solver_notes` when it is `None`.
- `assignment/app.py`: `publish :577-604` builds
  `AssignedHaulTripPayload(action=..., order=..., truck_id=tid, **compat.wire.wire_fields(tag))`
  and dumps `payload.to_wire_dict()`. The bytes are unchanged: dataclass `__dict__` order is the
  field declaration order, not the call's keyword order. The one keyword at `:549` becomes `solver_notes=`.
- `truck/app.py:381-393`: add `"solver_notes": parsed.solver_notes` to `collaboration` only when
  it is not `None` (trip docs unchanged otherwise).
- Tests: `test_share_accounting.py`, `test_container_logistics_message_data_models.py`, new
  `tests/test_solver_notes_wire.py`. The new test covers four things:
  1. bytes unchanged when every note carries only legacy keys;
  2. an extra key lands in `tag["solver_notes"]` and in the payload;
  3. an absent field is omitted;
  4. truck forwarding.

Gates: G-gold (published bytes: `test_golden_published_compare.py`), G-oog, G-full, G-vocab
(the `accounting.py` and `app.py` publish hits for `PAIR_NOTE_KEYS`, `pool_id`, `market_round`,
`_v1_tag` and `"v1"` go to zero; they now live only in `compat/wire.py`).
Done when: a solver can attach any JSON note to a shared pair with zero wire edits, under the
default top too (v1 tags carry `solver_notes` when present, and WS1 makes `Partitioned` merge its
inner's `solver_notes`), and every golden MQTT string is identical. `tick_notes` and `run_stats`
are TOP-only by design (message 24); the authoring guide says so (WS8). State the limit in the module docstring: notes surface only on
SHARED pairs (`accounting.py:154-192`; `truck/app.py:381` gates on `parsed.shared`).

### 7.7 WS1 — the core contract and ownership-by-declaration (base 35b5b92; rebase onto merge #4)

The long pole. One coder, five commits; each commit passes G-gold before the next starts.

**Commit 1.1: solver_lib skeleton (pure moves).**

- Create `policies/solver_lib/__init__.py`, a leaf docstring: the framework never imports it.
- Move `dispatch/helpers/candidates.py` → `solver_lib/candidates.py`,
  `dispatch/helpers/objective.py` → `solver_lib/objective.py`, and `dispatch/pair_solver.py` →
  `solver_lib/pair_solver.py`.
- Update importers by grep, not by list (L1):
  `grep -rnE "dispatch\.helpers|dispatch import helpers|from \.helpers|dispatch\.pair_solver|from \.pair_solver" apps openride openride_control tests`.
  This includes the sim main's local import at `run_container_logistics_simulation.py:146`.
- `tests/test_policy_import_cycles.py` `_LEAVES` (L1): replace `dispatch.plan` and
  `dispatch.pair_solver` with `dispatch.result`, `dispatch.bind` (both leaves) and
  `solver_lib.pair_solver`.

Gates: G-gold.

**Commit 1.2: contract types, not wired.**

- `dispatch/result.py` (`Result`, §2.6).
- `dispatch/bind.py` (`RunWorld`, `Declaration`, `BoundNode`, `BoundTree`,
  `bind_tree(node, top_instance, profile)`, `bind_profile(profile, instance=None)`, messages
  23/24, §2.2-§2.4).
- `dispatch/tick.py` (`Tick`, `Measure`, `owns_or_declared`/`permitted`/`serves` closures,
  `restrict`, `call`, `callee`, `docs`, `note_incident`, §2.5/§2.7).
- `invariants.check_result`.
- `assignment/constraints.py`: `declared_partnerships(profile) -> frozenset[tuple[str, str]]`.
- `compat/selection_keys.py`, `compat/measurement.py` (verbatim `runner.py:252-290`:
  `_ACCOUNTING_KEYS`, `accounting_params_for`, `framework_accounting_cost`; plus
  `accounting_dials(profile) = profile.get("solver_params") or {}`, `app.py:70`).
- Unit tests for each, in `tests/test_solver_contract_types.py`. They cover:
  - restrict id-subset refusal;
  - narrow composition;
  - sub-tick decision RNG = the callee instance's injection, while `sample_rng` is the
    caller's;
  - verified-pair memo (ownership half only);
  - `RunWorld.reads`;
  - message 23 (outside the ceiling) and 24 (a non-top stamp, or inner ⊄ top);
  - bind purity (bind twice → equal declarations; `random.getstate()` and a profile deepcopy
    unchanged).

**Commit 1.3: wire it and port the four built-ins** (the one big commit; split further if it
helps review, but every intermediate commit must pass G-gold).

- `dispatch/runner.py`:
  - `DispatchRunner(tree: BoundTree, capture=None)`;
  - `tick(*, candidates, orders, profile, clock, seed, execute) -> Tick`;
  - `run(tick) -> TickOutcome(pairs, result)`;
  - offload from `tree.top.spec.offload`;
  - message 18 unchanged;
  - `build_runner_for_app(instance, prof)` and `build_runner_for_policy(name, params)` keep
    their names and roles.
- `dispatch/profile.py`:
  - `build_policy_tree` without the `CompositePolicy` check;
  - `bind_instance`/`_bound_reused_tree` walk instance `inner` attributes only through the
    construction protocol; `isinstance` is replaced by "`spec` has an inner ref";
  - unregistered leaves go through `compat/unregistered.py` (`UnregisteredPairLeaf(instance,
    spatial: bool)`, the legacy rule of `profile.py:363-373` and the default decide of
    `pair_solver.py:58-79`);
  - `assert_assignment_profiles_resolvable` calls `bind_profile` (G18) and drops
    `validate_cooperation_world`;
  - `dispatch_node` stops computing `candidate_kind`.
- `dispatch/resolved.py`: delete `candidate_kind`, `KIND_WITHOUT_CANDIDATES_PARAM`,
  `candidate_kind_for`. `information_model`/`share_tag_format` stay until WS4b.
- `dispatch/base.py`: `solve_tick` shim + default `bind` (§2.2).
- `dispatch/composite.py`: `innermost_policy` deleted (callers use `BoundTree`).
- `framework/registry.py`: `offload` kwarg/field (§2.1); relaxed `_check_slot_base`.
- `solver_lib/cooperation.py`:
  - `Arm(id, adjacency, components, component_of, structure)`;
  - `resolve_arm(world) -> Arm | None`, verbatim from `constraints.active_cooperation_structure`
    + `share_adjacency` + `world._component_of` `world.py:57-69`;
  - `directed_pairs(arm) -> frozenset[(owner, carrier)]`;
  - `validate_arm(world)` = message 14 verbatim (`world.py:183-206`).
- `solver_lib/pair_leaf.py`: `PairLeafMixin.bind(params, world, *, top)`. At top it calls
  `resolve_arm` → `Declaration(cross_company=directed_pairs(arm))` + `validate_arm`. Nested, it
  returns `Declaration(candidate_hint="spatial" if params.candidates == "spatial" else None)`.
  - The leaf's own `candidates` param decides the path, as today (`greedy_nearest.py:98-103`).
    Supplied candidates are used ONLY on the spatial path (M6): `if params.candidates ==
    "spatial": pairs = tick.candidates if tick.candidates is not None else
    candidates.spatial_pairs(tick, settings)`. On the sampled path `tick.candidates` is ignored,
    and the RNG-consuming `candidates.sampled_trucks(tick, settings)` runs as today: per haulier
    from `tick.rng` at `depth == 0`, the whole slice from `tick.sample_rng` (or `tick.rng`) below.
  - `bind` reads the four candidate keys at top AND nested (M4).
  - `CandidateSettings.from_world(tick.world)`, same parse as `candidates.py:70-85`.
- `assignment/solver/greedy_nearest.py`, `random_assignment.py`: `solve_tick(tick, params)` +
  `PairLeafMixin`. Same bodies (`greedy_nearest.py:82-103`, `random_assignment.py:46-64`);
  `ctx.params` → `params`; `ctx.allowed_raw` → `tick.permitted`;
  `ctx.accounting_cost` → `tick.measure.cost` (RandomAssignment only; GreedyNearest keeps its
  own dial cost).
- `library/partitioned.py`: `bind` (top: arm pairs + `validate_arm`; it reads `cooperation`);
  `solve_tick` = per group (`cooperation_groups` moved in, reading `resolve_arm(tick.world)`):
  `tick.call(params.inner, tick.restrict(trucks=, orders=, scope=f"partition:{kind}:{value}",
  sample_rng=random))`. It merges `pairs` in group order, and merges each inner result's
  `solver_notes` (H6). It keeps `set_rng`/`set_tiebreak` non-forwarding (`:75-81`).
  `offload="children"`.
- `library/pooled_market.py`:
  - `bind` (top: arm pairs + `validate_arm`);
  - `solve_tick`: `self.inner.set_rng(tick.rng)`; `self.inner.set_tiebreak(tick.seed)` (kept: the
    determinism teeth tests patch `set_tiebreak`); `PoolMarket.from_structure(arm.structure)`;
  - a `_CompanyDecider` (the moved `ScopeDecider`):
    - `candidate_kind` = `tick.callee(p.inner).declaration.candidate_hint or "sampled"`;
    - `company_rule` = the moved `CompanyRule` (mtt from `tick.world.get("max_travel_time_pickup")`);
    - `company_context(...)` = `tick.restrict(trucks, orders, scope=f"company:{hid}",
      sample_rng=rng, candidates=prebuilt_pairs if kind == "spatial" else None)`, with
      `narrow=None` (pinned by WS0 pin 5);
    - `run(sub)` = `tick.docs`-mapped `tick.call(p.inner, sub)` (`[]` on `None`);
    - `report_crash` = message 20 text verbatim (`compose.py:213-221`) + `tick.note_incident()`;
  - returns `Result.from_pairs(result.assignment, solver_notes=result.pair_notes,
    tick_notes={...today's 5 keys...})`;
  - `offload="children"`;
  - cost stays `tick.measure.cost` in this WS (G2 is WS4a).
- `assignment/pooled_planner.py`: only the duck-typed `ctx.dispatch` contract changes name/shape
  if needed (`:163-185,296-321`). The engine logic is untouched.
- `assignment/app.py`:
  - `_build_dispatch_policy :125-133`; `_dispatch_runner :135-170` (cache key adds a digest of
    `{k: prof[k] for k in tree.world_reads}`, which covers the critique's "cooperation in the
    key"); `_dispatch_world :172-176` deleted;
  - `_dispatch_tick :506-558`, §2.7. Until WS4a it still feeds `result.tick_notes.get("market")`
    to `_record_round_stats`, keeping today's expression so the `market` count in `app.py`'s
    baseline does not grow. `_log_cooperation :557-575` is replaced NOW by the generic line
    `"Planner %s: %d cross-company assignment(s) this tick."`; reading the structure id would add
    a `cooperation` hit (H4). WS4a then only moves the round stats.
- `dispatch/capture.py`: format `/3` (`FIXTURE_FORMAT`). It records the `RunWorld` mapping (the
  whole menu + active arm), the top declaration (`cross_company` sorted lists, hints per path)
  and `live_pairs`; it drops `rule`, `information_model`, `share_tag_format`. `/2` is read via
  `compat/capture_v2.py` (structure → `world.cooperation {active, structures: [structure]}`);
  `/1` is still refused (message 22).
- `policies/testing/{fixtures,battery,bench}.py`:
  - fixtures build `RunWorld`/`bind_profile` from `world_profile(...)` (`fixtures.py:86-91`),
    with accounting dials via `compat.measurement`.
  - `Fixture.world()` (`fixtures.py:121-127`, unguarded) returns a `solver_lib.cooperation`-backed
    view exposing exactly what `battery.py:942-975` reads (`component_of`, `partners`), so the
    battery's information audit runs unchanged until WS4b deletes it (H4).
  - battery `_selection_instances` walks `BoundTree`.
  - A scope_log entry keeps today's shape `{kind, key, round, trucks, orders}`, with
    `kind = key.partition(":")[0]` (a generic string split of the solver-chosen key) and
    `round = None`. `battery._scope_problems` is therefore untouched in WS1.
  - `_probe_module_draws` is unchanged in intent (it injects private RNGs into instances whose
    `_rng is None`).
- Delete `dispatch/{context,scopes,world,allowed,compose,plan}.py` and
  `dispatch/helpers/__init__.py`.
- Tests: port the ~28 files of the "WS1 core" list (`grep -rlE
  "DispatchContext|scopes\.|tick_context|partition_context|company_context|DispatchWorld|compose\.|decide_in_scope|ScopeDecider|cooperation_groups|PartitionRule|CompanyRule|from_pairs|Plan\(|check_plan|CompositePolicy|PairSolver|innermost_policy|build_runner|DispatchRunner|verified_heads|proves_physical|tick_allowed"
  tests`). Explicitly, the 14 files that use deleted composite internals (`ctx.world.*`,
  `scopes.*_context`, `ScopeDecider`, `decide_in_scope`, `cooperation_groups`) and cannot run
  through the H1 adapter are rewritten against `tick.restrict/call/callee`:
  `tests/golden/capture_policy_golden.py`, `test_dispatch_capture_and_bench.py`,
  `test_dispatch_context_helpers.py`, `test_dispatch_plan_and_invariants.py`,
  `test_dispatch_profile_resolution.py`, `test_policy_battery_catches_broken_policies.py`,
  `test_policy_context_company_spatial.py`, `test_policy_final_review_battery.py`,
  `test_policy_final_review_runtime.py`, `test_policy_round2_c3_fixes.py`,
  `test_policy_round2_c3_review_fixes.py`, `test_policy_verification_review_fixes.py`,
  `test_random_spatial_candidates.py`, `test_whole_tick_dispatch.py`. Leaf-style `decide`
  policies elsewhere run through `compat.legacy_context` unchanged. Hand back a table of every
  edited assertion with the reason (M8). Rules:
  - keep every test's intent and teeth;
  - rename exact-message assertions only where the message text changed ("plan" → "result");
  - `test_policy_round2_c3_review_fixes.py:125-174` (the tick-check spy) becomes "decomposition
    pairs re-check only the ownership half; central pairs get the full predicate", spying on
    `pair_allowed_ignoring_haulier` and the new ownership closure;
  - `test_policy_context_company_spatial.py`: the "raises without the spatial kind" test becomes
    "builds its own spatial pairs when the caller supplied none".
  - In `tests/test_ownership_oracle_pins.py`, swap only `_sut_permitted`/`_sut_serves`/`_sut_groups`.
  - Update `tests/golden/capture_policy_golden.py` so it still runs (do not regenerate; G-gold's
    `git diff` covers the data files).
- `compat/legacy_context.py` (H1): `LegacyDecideContext(tick)` + the moved `Plan`, with
  `dispatch/plan.py` as a re-export shim until WS8.

**Commit 1.4: performance.** Ask the orchestrator to run G-perf (`SP/oog/perfdrive_ws.sh`,
serially on a quiet box; old = `SP/wt_base`, new = `$WT`) on
`A-greedy-partitioned-chain-spatial-cap500`, `A-greedy-pooled-chain-spatial-cap500` and
`C-greedy-partitioned-nocoop-only-sampled-cap40`. Pass: median ratio ≤ 1.05 per case. If the
pooled case exceeds it, re-run with `gc.collect(); gc.disable()` per sample (the doc 26 §7
protocol). Report both, plus the load average. Do not judge by wall time (CLAUDE.md §17/§24).

Gates (WS1 done): G-gold 101, G-oog 204/204, G-val with **counts exactly at baseline** (the
check set does not change in WS1), G-full, G-vocab (baselines of every touched framework module
shrink; `context`/`scopes`/`world`/`allowed`/`compose` disappear), G-perf.

Done when:

- no framework module names a cooperation structure, component, pool, rule type or candidate
  kind;
- the framework's predicate inputs are only `haulier_of` on the two documents, physical
  feasibility, and the frozen top declaration within the ceiling;
- every new test has a teeth note.

### 7.8 WS4a — run stats, the market's own bid cost (branch from merge #5; merge #6)

Files:

- `policies/dispatch/run_stats.py`: the protocol (§2.8), plus `patch_run_config_meta(app,
  key, value)` = `app.py:347-375` verbatim, generic key.
- `library/_market_stats.py`: `RoundStatsAggregator(meta_key="market")`. It is `app.py:274-316`
  (`round_stats`) + `:377-429` (`_record_round_stats`) verbatim; the cadence
  `_ROUND_STATS_PATCH_EVERY = 5` (`app.py:7-11`) and the "grew max" rule (`:340-346`) live in
  `push_due`. `clock.horizon_steps` replaces `self._sim_horizon_steps` (the same value,
  `app.py:535`).
- `library/pooled_market.py`:
  - top `bind` adds `run_stats=RoundStatsAggregator`;
  - G2: `bid_cost = objective.dual_cycle_cost_fn(bonus_km=<inner dual_cycle_bonus_km or
    constraints._DEFAULT_DUAL_CYCLE_BONUS_KM>, radius_km=<... or _DEFAULT_DUAL_CYCLE_RADIUS_KM>)`,
    passed as `PooledPlannerContext(cost=bid_cost)`;
  - the company sub-ticks keep the framework `measure` (unchanged).
- `compat/run_stats.py`: `LegacyAppAccessors` mixin with `round_stats(self)`, which returns
  `self._run_stats["market"].snapshot()` if present, else
  `RoundStatsAggregator.empty_snapshot()`. `AssignmentApp` inherits it.
- `assignment/app.py`: delete `:274-430` (`_log_cooperation` is already gone in WS1); `close :431-442`
  generic flush; `_dispatch_tick` folds and pushes generically and logs one INFO line
  (`"Planner %s: %d cross-company assignment(s) this tick."`).
- Tests: the round-stats tests (`test_assignment_pooled_convergence.py`, `test_whole_tick_dispatch.py`,
  `test_legacy_run_env_overrides.py`, `test_policy_override_equals_compiled_edit.py`,
  `test_policy_round2_c3_review_fixes.py`, `test_simulation_runtime_status_write.py`) move to
  the aggregator; a new `test_run_stats_contract.py` exercises a toy aggregator (generic key) and
  the market one (cadence, forced push on truncation, `close` flush, the backstop ERROR logged
  once).

Gates: G-gold (pooled `round_stats` bytes), G-oog, G-val (counts at baseline), G-full, G-vocab
(`app.py` baseline loses `round_stats`/`_record_round_stats`/`_patch_round_stats_to_run_config`/
`market`), G-perf (pooled only, run by the orchestrator).

Done when: `app.py` names no market concept, and the market's `cost=` is its own. State the G2
boundary in doc 26: pooled + a cost-blind inner + non-default `solver_params` now bids with the
constraint defaults.

### 7.9 WS4b — metadata, stamp, provenance, run names (branch from merge #5; rebase onto #6; merge #7)

Files:

- `framework/registry.py`: delete `INFORMATION_MODELS`, `SHARE_TAG_FORMATS`,
  `V1_SHARE_TAG_BUILTINS`, the `information_model`/`share_tag_format` kwargs and fields and
  their checks; add `badge`/`badge_tooltip`.
- `compat/wire.py`: add `V1_SHARE_TAG_BUILTINS = frozenset({("planner", "Partitioned")})` and
  `share_tag_format_for(name)`; `stamp_fields(top_name) -> {"share_tag_format": ...}`.
- `dispatch/runner.py`: the format comes from compat.
- `framework/resolve.py`: `ResolvedPolicy` loses the two fields (`:79,91-92,237-238`).
  `policies_stamp(..., tree: BoundTree, accounting_params, source)` writes `name, version,
  kind, timing, label, leaf, params, source`, plus `share_tag_format` (via
  `compat.wire.stamp_fields`), `accounting_params`, `provenance` (`{path: declaration.provenance}`),
  `declaration: {cross_company: [[o, c], ...]}` and `world_reads: [...]`. It no longer writes
  `information_model` or the top-level `candidate_settings`.
- `framework/catalog.py:74,78`: drop the two fields; add `badge`, `badge_tooltip`, `offload`.
- `framework/__init__.py`: exports.
- `framework/slots.py:69`: `timings=("online",)` (G13); `resolve.py:67` drops the `plan` entry;
  `runner.py:76-78` comment.
- `dispatch/resolved.py`, `dispatch/capture.py`, `policies/testing/bench.py:204,245` (column
  `badge`), `openride/policy_cmd.py:54,186-191` (prints `badge`).
- `library/partitioned.py`, `library/pooled_market.py`, `assignment/solver/*.py` decorators:
  remove `information_model`/`share_tag_format`; add `badge` (`"per group"` / `"per company"` /
  `"central"` when top) and `badge_tooltip` (text from fe `policy-slots.ts:645-658`).
  - `bind` provenance: `{"badge", "sample_scope", "candidate_settings"}` (values identical to
    today's `candidate_settings` + `sample_scope`).
  - Top `stamp_settings={"COOPERATION": solver_lib.cooperation.stamp(arm)}` when an arm exists.
  - `run_name_segments=(arm.id,)`.
- `solver_lib/cooperation.py`: `stamp(arm)` = `{structure_id, components, adjacency, pools}`
  (pools via `assignment.pools.pools_from_structure`, solver side).
- `assignment/pooled_planner.py`: delete `effective_cooperation_stamp :109-137` and
  `resolve_market_config :72-106` if no caller remains (G22).
- `policies/testing/battery.py`: delete `INFORMATION_MODELS`, `_model_declared`, `_group_key`,
  `_scope_problems`, `_scopes_respect_model` (`:101-131,927-1000`). Add the generic checks:
  - `contract.scope_audit`: runs the wrapper's optional `audit_scopes(scope_log, tick)` and
    SKIPs (with the reason) when it has none;
  - `contract.bind_pure`;
  - `contract.world_reads_at_bind`: keys read during `solve_tick` ⊆ keys read at `bind`;
  - `contract.declaration_within_ceiling` on every fixture.
  
  The `_scope_problems` logic moves into `Partitioned.audit_scopes` / `PooledMarket.audit_scopes`.
- Sim main:
  - `_stamp_effective_policies :140-182` uses `bind_profile` + the new `policies_stamp`; the
    print line `Policies effective: planner=<label> v<v> timing=<t> params=<json>
    accounting_params=<json>` (`information_model=` dropped); `sample_scope_for :184-194`
    deleted;
  - `:403-459` becomes `orsim_settings.update(top.declaration.stamp_settings)`, refusing a key
    collision with framework keys. The default-path run-name segment is appended from
    `run_name_segments` after the legacy segment, idempotent by segment (the same loop as
    `:441-445`). One generic print: `Run declaration: cross_company=<n> pairs, world_reads=<keys>`.
- Tests: the ~23 files of the info-model list; the message-15 test
  (`test_policy_framework_review_fixes.py:284-...`) becomes "v1 is compat's explicit name rule;
  no registration can opt in"; `test_assignment_pooled_convergence.py` (stamp).
- **Stamp oracle (M9), not self-certifying.** `SP/oog/stamp_base_35b5b92.json` was produced at
  base by `SP/oog/stamp_harness2.py`. It drives the real sim `main()` on `collab_smoke_12t` up to
  `push_run_status` for 11 input paths x {compiled, legacy profile} and records
  `(run_name, POLICIES, COOPERATION)`. In WS4b, run it in `$WT` and assert per case:
  - new `POLICIES.planner` = old − {`information_model`, top-level `candidate_settings`} +
    {`provenance`, `declaration`, `world_reads`};
  - `provenance[top]` = {`badge`} ∪ old `candidate_settings` (incl. `sample_scope`) — values equal;
  - new `COOPERATION` = old − {`topology`, `market`};
  - run names are identical on every case, and EXIT cases are still EXIT.

  Encode the assertion as a script in `SP/ws/ws4b/` (not a repo test, because the oracle file is
  scratch), and also add a repo test pinning the key set.

Gates: G-gold, G-oog, G-full, G-vocab (`registry`, `resolve`, `catalog`, `battery`, `bench`,
`capture`, `resolved`, sim main baselines → empty except WS8's re-exports). G-val: each exits 0
with PASS. Counts change (the `information.*` checks go; the new `contract.*` checks arrive);
report the old → new per-check delta table, and **no new SKIP on a built-in except
`contract.scope_audit` on the two leaves**.

Also check the run names. Use `SP/oog/stamp_harness2.py` (above) plus `SP/fin/rn_harness.py`
adapted to the 17 main-tree bundles x {default arm, `--structure` other arm,
`ORSIM_PLANNER_TOPOLOGY=pooled`, `--solver RandomAssignment`}, and diff against 35b5b92: it must be
identical. The same diff is re-run in WS8 with WS7's refusals as EXPECTED output (M10):
`--structure` on the 9 bundles without a cooperation block, and the `structure-typo` case, become
EXIT with message 26. Everything else stays byte-identical.

Done when: no framework file mentions information models, share formats or the market; the stamp
carries `provenance`/`declaration`/`world_reads`; run names are byte-identical; the analytics
planner rows still build (`COOPERATION.structure_id`/`components` present on cooperative runs).

### 7.10 WS7 — cooperation override fail-loud, world-read refusal, preflight, sharing removal (branch from merge #5; rebase onto #7; merge #8)

Files:

- `scenario/scenario_overrides.py` `apply_cooperation_override :554-611`:
  `(collection, structure) -> (applied_id, patched_world_keys: frozenset[str])`. An unknown id,
  or a structure requested on a scenario with no cooperation block, raises `PolicyError`
  **message 26**, naming the declared ids. The `sharing` parameter is removed.
- `dispatch/bind.py`: `refuse_unread_patches(tree, patched_world_keys)` → **message 25**.
  `profile.assert_assignment_profiles_resolvable(collection, *, patched_world_keys=())` calls it.
- Sim main:
  - `:365-383`: exit 2 on message 26/25 (via `_refuse_invalid_policy_config`); the override-path
    `· <id>` segment stays at `:376-381` (G10, byte-identical position);
  - refuse a set `ORSIM_SHARING_POLICY` (item 34) with "set planner.sharing in spec.json and
    recompile; the run-time override was inert";
  - order: the cooperation override now runs BEFORE `_assert_policies_resolvable_or_exit`
    (`:401`), so the refusal sees the patched profile. It already does: `:369` < `:401`. Keep it.
- `openride_control/policies.py`:
  - `validate_run_policy_args(..., cooperation_structure=None)` applies the override on the deep
    copy and refuses 26/25, so the dashboard gets HTTP 400 synchronously (keep the order pinned
    by `test_validate_run_policies_order.py`: names first, then the bundle);
  - new `run_preflight(scenario, *, cooperation_structure=None, solver=None, policies=(),
    params=(), overrides_json=None) -> {ok, action: "run_preflight", world_reads: [...],
    structures: {active, declared: [...]}}`, which always reads the bundle.
- `openride_control/command.py`: wire `validate-run-policies --cooperation-structure` and
  `run-preflight`.
- Remove `sharing_policy` plumbing: `openride/runner.py:86-142`, `openride/run_group.py:146,212`,
  `openride/run_cmd.py:102`, `openride_control/manager.py:462-504`. `--sharing-policy` must
  fail with "removed: inert at run time".
- Tests: `test_validate_run_policies_order.py`, `test_pool_housekeeping.py`,
  `test_legacy_run_env_overrides.py` (sharing parts), `test_policy_run_overrides.py`, plus new
  ones for messages 25/26, preflight output, and slug matching kept (`structure_id_slug`,
  `:571-576`).

Gates: G-gold, G-oog (unaffected: no structure override in profiles), G-val, G-full, G-vocab
(sim main/`scenario_overrides` baselines contain only WS8 re-exports).

Docs note (M10): `scripts/broker_gate.sh` REQUIRES `--structure` (CLAUDE.md §8). After WS7
it can no longer gate a scenario without a cooperation block, because `--structure` is then
refused (message 26). Document this in doc 26 and in the script's usage text; changing the
script itself is out of scope.

Done when: a typo in `--structure` refuses the run before spawn and in the dashboard dry run; a
tree that reads no `cooperation` refuses an explicit structure (exercised with an in-test toy
leaf under `isolated_registry`); preflight returns `world_reads` that include `cooperation` for
all four built-ins on a cooperative bundle.

### 7.11 WS6 — frontend (FE repo, base 6f9ca8b; developed in Phase 1-2, merged after merge #9)

```bash
git -C /home/user/openride_server/analytics worktree add /home/user/wt/ws6-fe -b ws/ws6-fe 6f9ca8b
ln -s /home/user/openride_server/analytics/node_modules /home/user/wt/ws6-fe/node_modules
```

Contract it codes against (backend fields, §7.9/§7.10):

- catalog entries gain `badge`, `badge_tooltip` and `offload`, and lose `information_model` and
  `share_tag_format`;
- the stamp gains `provenance{<path>: {badge, sample_scope, candidate_settings}}`, `declaration`
  and `world_reads`, and loses `information_model` and the top-level `candidate_settings`;
- `run-preflight`;
- `validate-run-policies --cooperation-structure`.

Files:

- `lib/policy-catalog-types.ts` (`:61-84,141-149`, new fields optional).
- `lib/policy-slots.ts`:
  - `informationModelLabel :661-672` becomes `policyBadge(entry)`: `badge`/`badge_tooltip` →
    else `information_model` via the old table (`:645-658`, kept only as the fallback for an
    older backend);
  - `centralPlannerNotice :710-722` uses `policyBadge`.
- `components/scenario/PolicySlotsEditor.tsx:41,70,152`.
- `lib/compareRunMeta.ts:1,35,66,166`: `dispatchInformation` = `provenance[top].badge` → else
  `information_model` (old runs).
- `lib/runNameMeta.ts:83-95`: comment `KNOWN_TOPOLOGIES` as compat (old run names); no logic
  change.
- `components/scenario/SimulationTab.tsx:84-110,346-375`: fetch `/api/scenario/run-preflight`.
  When `world_reads` is present and lacks `cooperation`, disable the dropdown and show "The
  selected solver does not read the cooperation structure, so every arm would run the same".
  With the field absent (older backend), keep it enabled.
- New `app/api/scenario/run-preflight/route.ts` (execs `run-preflight`).
- `app/api/scenario/run/route.ts:37-55,160-161`: pass `--cooperation-structure` to
  `validate-run-policies` (400 on `ok:false`) and remove `sharingPolicy`.
- Tests: `*.test.ts` for each lib change, plus a SimulationTab gating test.

Gates: G-fe: tsc 0; vitest ≥ 601 + new, 0 failed. `/verify-frontend` after the backend merge.
Playwright screenshots (CLAUDE.md §8 browser notes) of the scenario Policy step (badges) and the
Run step (dropdown enabled on a cooperative scenario; disabled with the reason when a test-only
non-reading leaf is selected), and Compare on one pre-change run and one post-change offline
stamp fixture.

Done when: the FE renders old and new catalogs and stamps alike, and dropdown gating works
against the real backend.

### 7.12 WS8 — finish: shims out, guard final, litmus, docs (base = merge #8)

Files:

- Delete the shims: `dispatch/legacy.py`, the re-exports in `profile.py`/`scenario_overrides.py`.
  Update every importer (tests: `grep -rlE "dispatch\.legacy|from .legacy|check_mirrors|mirror_dispatch_selection|apply_planner_topology_override|apply_market_override|known_planner_topologies|legacy_topology_name" tests apps openride openride_control`).
- Delete `tests/vocabulary_baseline/`. `test_framework_vocabulary.py` asserts **zero** hits, with
  the exempt set exactly `policies/compat/**`.
- `tests/test_solver_contract_litmus.py` (litmus a, in-process, under `isolated_registry`,
  through `AssignmentApp.assign`/`publish` with the golden harness's stubs copied, not
  imported). A toy composite and a toy leaf, defined in the test, ~60 lines:
  - the composite reads its own world key `litmus_zones` at bind (the profile carries it);
  - it declares cross pairs ⊆ the scenario partnerships;
  - it splits by zone and calls the leaf via `tick.call(params.inner, tick.restrict(...))`;
  - the leaf attaches `solver_notes={"litmus_reason": "nearest"}`;
  - the composite returns `tick_notes` folded by its own `run_stats` class (`meta_key="litmus"`,
    PATCH captured by a stub session).

  Assert:
  1. the published JSON of a shared pair carries `"solver_notes": {"litmus_reason": ...}` and
     a non-shared one carries no `solver_notes` key;
  2. `meta.litmus` is patched;
  3. the stamp carries the toy's provenance and `world_reads ⊇ {"litmus_zones"}`;
  4. declaring a pair outside the ceiling → message 23;
  5. an inner widening → message 24;
  6. message 25, using a SEPARATE tree: a toy leaf `NonReader` that reads no world key, selected
     as a central top on a cooperative profile, with `--structure` applied → message 25. The toy
     composite above cannot serve here, because it must read `cooperation` to declare its pairs
     (L2).

  The test imports only `policies.framework`, `policies.dispatch.{tick,result,bind,run_stats}`
  and `assignment.app` (assert that set with `ast`).
- `tests/test_solver_contract_dropin.py` (litmus b). Copy the CHECKED-OUT tree, uncommitted edits
  included (`git ls-files -z | xargs -0 cp --parents -t tmp_path`; `git archive HEAD` would test the
  last commit, not the working tree, L2), write the same toy leaf as
  `apps/container_logistics/policies/dispatch/library/litmus_toy.py`,
  then in a subprocess with cwd = the copy:
  - `python -m openride policy validate planner LitmusToy` → exit 0, PASS;
  - `python -m openride policy validate planner Partitioned` → exit 0, and its output includes
    the derived arm `inner=LitmusToy`. Ref arms come from `battery.policy_arms` /
    `_ref_arm_targets` (`battery.py:231-257`), not from `_composite_targets` (`:1075`), which lists
    composites (L2).

  This proves real discovery (G11) with zero framework edits. Mark it `slow` if the suite has the
  marker, else keep it (~60 s).
- **`tests/test_solver_contract_rename_litmus.py` (litmus c, the rename litmus).** Litmus (a)/(b)
  prove a NEW solver is served; this one proves the framework has no name-keyed branch for the OLD
  ones. Under `isolated_registry`, register subclasses of the four built-in classes under fresh
  names: `RenamedPartitioned(Partitioned)`, `RenamedPooled(PooledMarket)`,
  `RenamedGreedy(GreedyNearestSolver)`, `RenamedRandom(RandomAssignmentSolver)`. Subclasses are
  needed because one class may back only one name (`registry.py:419-424`); each has the same
  `@policy` metadata except `name`, and they inherit `Params`.
  - For every one of the 68 `dispatch_golden_v1` cases, retarget the profile:
    1. resolve the original legacy profile with `resolve_profile_policies`;
    2. rename every planner type in the resolved block;
    3. write it as a compiled `policies` block + `policies_format: 2`;
    4. set `strategy` to the renamed leaf;
    5. drop `planner.topology`, `planner.market` and `use_spatial_matching` (it is refused next to
       a block, `profile.py:214-219`; the translation already put `candidates="sampled"` into the
       leaf's params).
  - Run each case through the harness's `run_case` with `case["strategy"]` set to the renamed
    leaf. The harness is imported read-only.
  - Assert every output (pairs, round_stats, rng_probe, published, share_tags) equals the stored
    golden expectation, except the DOCUMENTED compat name-keyed rule. Partitioned-topped cases
    with shares then render the `audited` format, because v1 is keyed on the name `Partitioned`
    (`compat/wire.py`). For those cases the test asserts the pairs/rng/round_stats equality, and
    that the tag VALUES (owner, carrier, benefit) equal the golden ones.
  - Any other difference means the framework branches on a solver name, and the test fails.
  - Note: the legacy translation also names solvers, but it is not exercised (the profiles are
    compiled blocks). That is exactly its documented compat role.
- Docs:
  - `docs/policy_authoring.md`: the minimal contract (§2.5 box), "advanced", messages 23-26,
    `solver_notes`, `run_stats`, `candidate_hint`, single-inner limit, the badge.
  - `/home/user/docs/reference/features/26-policy-slots.md`: rewrite §2 boundary table, §3.3
    stamp, §3.4 tags (compat), traps 3/4/13/15, §8, and a new "generic contract" section with
    the G2 boundary.
  - `21-shared-pool-cooperation.md` §2a: "every run whose top solver reads `cooperation` names
    its arm".
  - `/home/user/CLAUDE.md` §5 doc-26 line and §6.27 (tell arms apart by `label` /
    `provenance`; `information_model` exists only on runs stamped before this change).
  - Re-run `uvx --from 'memsearch[onnx]' memsearch index docs/reference --collection ms_user_b98d692c`.

Gates (final, on the merge commit of #9):

- G-gold, including the `git diff` of the golden inputs;
- G-oog with the profiles regenerated (`mkprof2.py` + `cmp`, M2);
- G-val, all four PASS with exit 0, valtable diff = the WS4b delta table;
- G-full: no new failure; skipped set and passed count as declared; the litmus (a)/(b)/(c) tests
  pass;
- G-vocab: zero hits and zero tagged lines; the import rule at its end-state allow-list;
- the import-cycle test;
- the run-name/stamp diff (`SP/oog/stamp_harness2.py` + the bundle run-name harness) against
  35b5b92, with WS7's refusals as the expected EXIT outputs (M10).

Done when: every gate is green on one commit, the guard's only exemption is `compat/`, and both
litmus tests pass with no framework file edited in their commits.

## 8. Ownership/byte-identity proofs carried by the plan

| Must stay identical | Held by |
|---|---|
| The pair predicate truth table | WS0 pin 1 (oracle) before and after WS1 |
| Spatial eligibility (`serves`) | WS0 pin 2; `share_eligible` is directional (`constraints.py:354-363`), so `(owner, carrier) ∈ declared ⇔ owner ∈ adjacency[carrier]` |
| Group order | WS0 pin 3 |
| PooledMarket company feasibility with `narrow=None` | WS0 pin 5 + the argument in §2.5. A company sub-tick holds only that carrier's trucks (`pooled_planner.py:425-429,470`), so `CompanyRule.allowed` (`allowed.py:91-99`) ≡ physical ∧ (own-order ⇒ own truck) ≡ `permitted` whenever pools ⊆ adjacency (message 14) |
| RNG objects/draw order | sub-tick decision RNG = callee instance injection (unchanged); `sample_rng` explicit (Partitioned: `random` module; PooledMarket: `tick.rng`); top `Random(seed)` lazy; `set_rng`/`set_tiebreak` calls kept in PooledMarket |
| Share-tag bytes, published payloads | WS3 (omitted-when-empty field; key order unchanged); G-gold `test_golden_published_compare` |
| Round stats | WS4a verbatim move; G-gold pooled cases call `app.round_stats()` via the compat shim |
| Run names | WS2 (legacy segments) + WS4b (`run_name_segments`) + the WS4b run-name/stamp diff against `SP/oog/stamp_base_35b5b92.json`, re-run in WS8 with WS7's refusals expected |
| No name-keyed framework branch | WS8 rename litmus (c) over all 68 golden cases; only the compat v1 rule differs |
| Bundles | no compile output changes (mirrors/echoes still written, WS2) |

## 9. Risks

### 9.1 Verified (this pass)

- Golden counts 68 / 15 / 40 (heads) and 101 golden tests total; validate counts 47 / 45+2 /
  44+3 / 179+8; FE 601/601, tsc 0; full suite 37 F / 8 E with the exact 45-line set. All
  re-measured at 35b5b92.
- Out-of-grid reproducible at 35b5b92: 204 entries, 0 errors (`SP/oog`).
- The full-suite command needs `--continue-on-collection-errors`; without it pytest stops after
  collection ("Interrupted: 8 errors during collection"). A gate run without the flag would read
  as "8 errors" and silently test nothing.
- All golden worlds and all 17 bundles: pools ⊆ adjacency, symmetric adjacency, ceiling ⊇
  active edges. All dials are at the defaults, so G2 (WS4a) is byte-identical on every gate
  input.
- No shipped solver emits a tail; tails appear only in `test_dispatch_plan_and_invariants.py`
  unit tests.
- Analytics tolerates an absent `COOPERATION` (`analytics/manager.py:74-75`); no other reader
  of `COOPERATION.topology/market` exists (grep over `openride/`, `openride_control/`,
  `apps/dataplane/`, the FE).
- The FE never reads `COOPERATION` (only a comment, `runNameMeta.ts:10`).
- The golden harness (frozen) calls `AssignmentApp.__new__`, `_solver`, `_solver_params`,
  `_share_tags`, `assign`, `publish`, `round_stats`, `get_solver`
  (`tests/golden/harness.py:88-139`). Every new app attribute must be created lazily
  (`getattr(self, "_x", None)`), and `round_stats()` must survive (compat shim).
- The only test that monkeypatches a constraint function patches
  `pair_allowed_ignoring_haulier` (`test_policy_round2_c3_review_fixes.py:152`). The new
  predicate calls it through the module, so the patch still lands.
- The determinism teeth tests neuter `BaseAssignmentSolver.set_tiebreak`
  (`test_assignment_pooled_determinism.py:346-359`). That is why PooledMarket keeps calling
  `set_rng`/`set_tiebreak` on its inner instance instead of passing the RNG through `restrict`.

### 9.2 Unverified (owner and how to close)

| Risk | Closed by |
|---|---|
| `tick.call` overhead on PooledMarket (companies x rounds calls per tick, ≤ 20 rounds) | G-perf in WS1/WS4a, direct timing |
| Re-entrant component lookup during discovery for a third-party policy that references a component kind | WS5 cold-interpreter test; documented |
| `bind` purity across the two processes (sim main vs celery agent) | WS1 unit test + WS4b `contract.bind_pure` battery check. A digest cross-check through `orsim_settings` is not built (§11) |
| Run-name byte identity on every input path | WS4b run-name diff over 17 bundles x 4 paths |
| The vocabulary lists producing noise or missing a real hit | WS0 manual review of the baselines; WS8 zero |
| The capture `/2` → `/3` reader on real captures | WS1: replay `SP/fin/cap` captures if present (`policy bench --fixtures <dir>`) |
| Test churn (~50 files) hiding a weakened assertion | each WS hands back a list of edited assertions with the reason; the reviewer diffs exact-message tests |
| Live behaviour (celery, real runs) | out of scope: the branch is not live (doc 26 §7 pending gates still apply after merge) |

## 10. What the critique's holes became

- H1 → G1 channel (WS1).
- H2 → ceiling (WS1).
- H3 → only top declares + `top` flag (WS1).
- H4 → G2 (WS4a) with the boundary stated.
- H5 → `bind_profile` everywhere + purity tests (WS1/WS4b) + the cache key on read keys (WS1).
- H6 → two litmus tests (WS8).
- H7 → pair memo, not a `physical` memo (WS1).
- H8 → minimal-surface docs (WS8).
- H9 → two RNGs (WS1).
- H10 → only top stamps (message 24).
- H11 → preflight + FE gating + doc 21 (WS7/WS6/WS8).
- H12 → G22 deletes the echo dependency; WS ordering in §7.2.
- H13 → `own` = `haulier_matches` semantics.
- H14 → `ast` guard with tiers and baselines (WS0).

## 11. Explicitly out of scope / later phases

- **Dials → world measurement setting** (decision b, last step): changes compile output (16
  recompiles) and makes `benefit_km` stop following a `--policy-param` dial override. That is a
  separate decision.
- Stopping the `planner.topology`/`planner.market` echoes in bundles; removing the legacy
  translation once the v1 golden is re-captured against `policies` blocks; v1 → audited tag
  unification (P7: a wire boundary).
- NetworkFlow (separate spike), the `plan` timing, `ctx.travel`/facilities, lifting single-inner
  nesting, a declaration digest cross-check between the sim main and the agent, stamping
  violations/crashes in `run_config`.

## §R Revision log (fixes for `critique_contract_final.md`)

Each fix was checked against the code at 35b5b92 before it was written. The user's binding
decisions are unchanged. No finding was rejected, but one is narrowed (H1). The workstream
split is unchanged. Only how Phase 2 is scheduled is clarified (M10).

| ID | Fix | Where | Verification |
|---|---|---|---|
| B1 | The wrapper entry point is named **`solve_tick(tick, params)`**. The pair API `solve`/`solve_pairs` is left untouched. Registration looks for `solve_tick`, and `DispatchPolicy.decide` stops being abstract. | §2.1 "Why `solve_tick`", §2.2, §2.7, §7.7 | `pair_solver.py:47-56` (abstract pair `solve`); `greedy_nearest.py:105`, `random_assignment.py:66`; 7 test call sites |
| H1 | **Narrowed, not rejected.** `compat/legacy_context.py` adapts the LEAF-style ctx members, and the old `Plan` moves into it, with a shim at `dispatch/plan.py` until WS8. The default PairSolver decision is respelled as `solver_lib.pair_solver.PairSolver.solve_tick`. The 14 test files that use deleted composite internals (`ctx.world.*`, scope builders) are named in WS1's explicit port list; the "keeps ~46 working" claim is dropped. | §2.2, §7.7 Tests | grep: `ctx.world` in 5 files, scope builders etc. in 14 files; 45 `def decide` |
| H2 | Identifiers and strings are split on `_`, camel-case and non-word characters, then matched against banned subwords (incl. `pooledmarket`). A new `tests/test_framework_imports.py` adds: forbidden packages for tier A; a `(module, symbol)` allow-list for imports from `compat`; and an allow-list of the `constraints` functions tier A may call. | §7.3 | tier-A `constraints` calls enumerated today (19 names, incl. 3 cooperation readers) |
| H3 | The audited renderer, the v1 renderer and `wire_fields(tag)` all move to `compat/wire.py`. `publish` passes `**wire_fields(tag)`, which keeps the same bytes because dataclass `__dict__` order is field order. | §7.6 | `accounting.py:206-224`, `app.py:588-598` |
| H4 | `_log_cooperation` is replaced in WS1. `Fixture.world()` returns a `solver_lib`-backed view, so `battery.py` stays unchanged until WS4b. Scope-log entries keep `kind` (= key prefix). Transitional baseline lines need a `# removed-by: WS<n>` tag. | §7.3, §7.7, §5.1 row 21 | `battery.py:942-975` reads `component_of`/`partners` |
| H5 | G-gold adds `git diff --exit-code 35b5b92 -- tests/golden/{*.json.gz,harness.py,compare.py}`. The false "after 277e67b" wording is removed. | §7.0, §7.1 | `git log`: `2a1ccf2`, `4f01034`, `3d7e84e` touch golden files |
| H6 | v1 tags carry `solver_notes` when present. `Partitioned` merges its inner's `solver_notes`. `tick_notes`/`run_stats` are documented as top-only. | §7.6, §7.7 | `partitioned.py:85-90` merges chains only |
| M1 | The pin-1 teeth mutate `PartitionRule.allowed`, not `structure_allows`. | §7.3 teeth | `world.py:105-107` → `allowed.py:48-62` |
| M2 | WS2/WS8 regenerate the profiles with `SP/oog/mkprof2.py` and `cmp` them against `prof.json`. | §7.1 G-oog, §7.12 | `mkprof2.py` at base: byte-identical |
| M3 | New `SP/oog/perfdrive_ws.sh <wt> <out>` with a private output dir. The orchestrator runs it serially on a quiet box. | §7.1 G-perf, §7.7 | the old `g56/perfdrive.sh` hardcodes `wt_old`/`wt_head` and shared outputs |
| M4 | Read rules: `bind` reads every key `solve_tick` reads (listed per built-in). A frozen `world_reads` snapshot. A sentinel in the cache digest. Capture/stamp never iterate `RunWorld`. | §2.4, §7.7 | the PooledMarket `CompanyRule` mtt and the prebuild knobs are read at solve |
| M5 | Measurement dials: live = `app._solver_params`, offline = `accounting_params_for(tree)`. | §2.5 `measure` | `app.py:70,176`, `harness.py:107` |
| M6 | Supplied candidates are used only on the leaf's spatial path. | §7.7 pair leaf | `greedy_nearest.py:98-103` |
| M7 | `call` refuses a sub-tick it did not make, or one already used. `restrict` always returns a fresh sub-tick. | §2.7 | – |
| M8 | G-full diffs the skipped set and checks the passed count; G-val diffs the per-check id/status table (`SP/oog/valtable.sh`). WS1 hands back a table of edited assertions. | §7.1, §7.7 | baselines: 5 skipped; valtables 47/47/47/187 |
| M9 | The stamp oracle comes from the base: `SP/oog/stamp_harness2.py` → `stamp_base_35b5b92.json`, with explicit old→new key algebra. | §7.9 | ran at 35b5b92: 22 cases (11 paths x 2) |
| M10 | Phase 2 is developed in parallel from #5 and merged sequentially #6→#7→#8 (headings fixed). The run-name diff is re-run at WS8 with message-26 refusals expected. The `broker_gate.sh` limitation is documented. | §7.2, §7.8-§7.10, §7.12 | base oracle: `structure-typo` currently runs silently as `pair` |
| M11 | Decision (f) is enforced by `tests/test_framework_imports.py`. | §7.3 | – |
| L1 | Importers are updated by grep, which includes the sim main's `:146`. `_LEAVES` is updated (`result`, `bind`, `solver_lib.pair_solver`). | §7.7 commit 1.1 | `run_container_logistics_simulation.py:146` |
| L2 | Litmus (b): arms come from `policy_arms`/`_ref_arm_targets` (`battery.py:231-257`), and the copy is taken from the working tree via `git ls-files`. Litmus (a) item 6 uses a separate `NonReader` tree. | §7.12 | `battery.py:1075` lists composites |
| L3 | No fold on a message-18 discard. | §2.7 | today's `TickOutcome(pairs=[], notes={})` |
| L4 | A collision is any `stamp_settings` key already in `orsim_settings`; it is refused. | §2.3 | – |
| L5 | Stated why `candidate_hint` sits on the Declaration (it depends on params). | §2.3 | – |
| L6 | `child.instance` is the object at `parent.inner`, and that identity must be preserved. | §2.7 | `profile.py:343-344` |
| rename litmus | `tests/test_solver_contract_rename_litmus.py`: the four built-ins as renamed subclasses, all 68 golden cases retargeted. Only the compat v1 name rule may differ. | §7.12, §8 | `registry.py:419-424` (one class backs one name, hence subclasses) |


---

# Appendix A — final adversarial review, round 1 (on 44922b4)

## Final adversarial review — backend 35b5b92..44922b4, FE 6f9ca8b..ea12f20

Counts: BLOCKER 0 · HIGH 3 · MEDIUM 15 · LOW/NIT 14. Probes: review_final/{me,own,failloud,tests/probe,fe}/

No way found for a DEFAULT run to differ from 35b5b92: the goldens, plus 515 extra base-vs-HEAD
differential cases, plus run names over 4 scenarios × 8 environment paths. The weak points are:
- ownership holds only against an honest solver;
- "speaks no solver's language" holds only lexically.

## HIGH

**H1 — `bind` can widen the ceiling, and the widened profile reaches the agents.** CONFIRMED.
- Where: `bind.py:321` (`prof = dict(profile)`, a shallow copy) · `bind.py:80-81` (`RunWorld` is a shallow `MappingProxyType`) ·
  `bind.py:337-343` (`bind` runs first, then `_check_top` computes the ceiling from the same nested objects).
- Repro: `bind` does `world["cooperation"]["structures"].append({"id":"evil","adjacency":{"A":["B"],"B":["A"]}})` and declares the pair.
  - Message 23 passes, and the cross-company pair is committed and published as shared.
  - The mutation persists into the sim main's assignment collection, which is shipped to the agents.
  - Each further bind appends again: `['pair','solo','evil','evil']`.
- Probes: `me/probe_tamper.py` (mode `worldmut`) and `own/probe_commit.py`, which goes through `AssignmentApp.assign` and `publish`.
- Only the opt-in battery check `contract.bind_pure` catches this.
- Fix: compute the ceiling from a deep copy taken BEFORE any bind. Deep-freeze the `RunWorld` values. Refuse a profile whose digest changed across binds.

**H2 — the framework's final check reads `Tick` state the solver can rewrite.** CONFIRMED.
- Where:
  - `tick.py:255`: `permitted` is a public, writable attribute.
  - `tick.py:479`: `_top_check` reads `self.permitted` after the solve.
  - `tick.py:248-249,332-341`: `raw_trucks`/`raw_orders` are writable, and `id_index` is a lazy `cached_property` over them; `docs()` commits from it.
  - `tick.py:174-181`: `haulier_of` is read from the live dict at check time.
- Each of these committed an undeclared or infeasible pair with `violations=0` and no message 18:
  1. `tick.permitted = lambda t, o: True`.
  2. Pre-seeding `tick.__dict__["id_index"]`, which publishes a forged order doc.
  3. Reassigning `raw_trucks`.
  4. An in-place `truck["haulier_id"]=...`: the pair is accounted `shared=False`, hiding a cross-company haul from the research metric.
  5. An inner overriding `sub.permitted` under `Partitioned`: the verified-pair memo then skips the physical check, so a 20ft truck took a 40ft order.
- `tick.permitted` is part of the documented minimal author surface, and the `invariants.py` docstring's claim is false.
- Fix: make these read-only properties over framework-held values. Check against a privately held predicate (a closure). Snapshot hauliers and the physical inputs, keyed by `id()`, when `Tick.top` is built.

**H3 — the vocabulary guard is blind to `RandomAssignment`, and no litmus case has a central top.** CONFIRMED by mutation.
- Where: `test_framework_vocabulary.py:58-61,135-150`.
  - The camelCase split gives `random` + `assignment`, and neither is banned.
  - `scan_source('if spec.name == "RandomAssignment": pass')` returns `{}`.
  - The same blindness applies to `p["candidates"]`, `max_rounds` and `inner`.
- All 68 golden and rename-litmus cases are topped by `Partitioned` or `PooledMarket`.
- Mutation: in `app.py::publish`, drop one published assignment when the top is `RandomAssignment`. The guards, litmus a/b/c and goldens all pass, and the whole suite passes except 4 failures that are pre-existing.
- Fix: build the banned set from the REGISTRY (names, param names, component names). Match whole identifiers and string literals. Add central-top cases that go through `publish`.

## MEDIUM

**M1 — the `stamp_settings` collision guard misses keys that `main()` writes later.**
- `STREAM_GEO`, `PERSIST_ROUTE_GEO` and `ORDER_LIFECYCLE` are accepted; `POLICIES` and `MIN_STEP_WALL_TIME_MS` are refused.
- The docstring says `STREAM_GEO` is refused, and no test pins the guard.
- Where: `run_container_logistics_simulation.py:224-231` vs `:285/:471/:481/:498`. Probe: `failloud/clash_probe.py`.

**M2 — a run-stats `meta_key` is never validated** (`run_stats.py:73-75`). `meta_key="policies"` would PATCH over the framework stamp.

**M3 — the analytics planner rows and gains attribution depend on the solver choosing to stamp `COOPERATION`** (`run_container_logistics_simulation.py:231`; `analytics/manager.py:69-81,1382`).
- A contract-only solver gets no planner rows.
- A solver can stamp components that differ from what it declared.
- Fix: derive the stamp from the active arm in world data.

**M4 — the FE reads a solver's `run_name_segments` as the structure id** (`lib/runNameMeta.ts:203-236`). It takes the first unclassified segment, so a third-party segment like "zones3" is mislabelled.

**M5 — information containment is now self-certified.**
- The framework-owned per-model audit was replaced by the solver's own `audit_scopes`, which is SKIPped when absent.
- A leaky per-company solver passes every check, so the "per company" badge is unverified text. Probe: `tests/probe/test_probe_leak.py`.

**M6 — the G2 bid-cost change is wider than documented, and the goldens are blind to it.**
- Documented case: RandomAssignment + pooled + off-default `solver_params`.
- Undocumented case: a bundle whose `solver_params` lacks the dial keys, plus `--policy-param planner.inner.dual_cycle_bonus_km=X`. The mirror rule only rewrites keys that are present, so the old bid used the defaults and the new bid uses X.
- Not exposed on shipped bundles. Record it as a comparability boundary in CLAUDE.md §6.27.

**M7 — `DispatchPolicy.bind` reads a built-in's param** (`base.py:164-165`, `candidates == "spatial"`), so a third-party solver gets a hint it never declared.

**M8 — the framework still executes solver-specific rules via compat, or leaves them in place.**
- Via compat:
  - `share_tag_format_for(name)` (keyed on Partitioned, every run);
  - `DEFAULT_PLANNER`;
  - `round_stats` / `"market"` through `LegacyAppAccessors`;
  - the greedy dial names in `Measure.cost`.
- `offline_measurement_dials` is a rename of the banned `accounting_params_for`, made to pass the guard.
- Legacy keys are still read in `app.py:66,151-157` and `profile.py:151-155`.
- `compat.measurement.accounting_dials` and `framework_accounting_cost` have zero callers.

**M9 — import-guard holes.**
- It misses dynamic, aliased and `sys.modules` imports, attribute traversal, and re-export laundering.
- The allow-list is global, not per module.
- Tier A reaches `solver_lib` transitively (`testing/fixtures.py`, `compat/unregistered.py`).
- A new framework module is unscanned unless someone adds it to a tier.

**M10 — litmus (c) is narrower than claimed.**
- It catches only branches that treat unknown names differently; it is blind to `isinstance` and param-shape branches.
- It does not compare run names or the stamp.
- In its 13 v1-exception cases, `awarded_cost_km` and `owner_reserve_km` are copied from the actual tags.
- Litmus (b) never exercises `assign` or `publish`.

**M11 — with no flag, the dry run checks nothing.**
- `openride_control/policies.py:194-195` returns early, and the FE's `runPolicyOverrides.ts:61` skips the dry run.
- `SimulationTab` treats a preflight `ok:false` as fail-open.
- A stale bundle shows "Simulation starting…", then the sim main exits 2 out of sight.

**M12 — the agent binds lazily and never checks `policies_format`.**
- Lazy bind (PLAUSIBLE): an agent-only bind failure is logged every tick, and the run completes with no assignments.
- Format (CONFIRMED): a round-1 flat block binds as a central GreedyNearest.
- Reachable through a stale worker or the smoke/stress paths.

**M13 — a contract-only solver's stamp has no badge.** The provenance is `{"planner": {}}` even with `@policy(badge=…)`, while `policy_authoring.md:143-147` claims it stamps `central`.

**M14 — the tracked `docs/plans/26-policy-slots.draft.md` is stale.** It still describes `information_model`, `DispatchContext`, `ctx.world` and the deleted modules, yet claims to be identical to the doc.

**M15 — the structure-dropdown gating has no component test and no browser evidence.** Vitest includes only `lib/**`, and the disabled path cannot be reached with the shipped solvers.

## LOW / NIT

- **L1:** a malformed `cross_company` raises `TypeError`, not message 23. Non-`PolicyError` bind exceptions exit 1, not 2.
- **L2:** `RunWorld` reads can go untracked (`unread()`, `_data`, `_reads.clear()`). Splatting the world (`dict(world)`, `**world`) marks every key read, which silently disables message 25.
- **L3:** `solver_notes` can forge `pool_id` / `market_round` on the wire. Non-JSON note values are published with `default=str`, which differs from run to run.
- **L4:** message 25 works per top-level key, and the ceiling is the union of all arms. The framework never enforces that the solver uses the ACTIVE arm.
- **L5:** an inner's `cross_company` declaration is checked but inert: it never narrows that inner's sub-tick.
- **L6:** `tick.serves(None, None)` returns True.
- **L7:** a buggy aggregator `fold` crashes the assignment tick, because `record_tick` is not wrapped.
- **L8:** inherited `ORSIM_*` env vars reach the sim main but not the dry run. A stray `ORSIM_SHARING_POLICY` makes every run exit 2.
- **L9:** `apply_cooperation_override` gives no message 26 when there are no assignment agents.
- **L10:** `scenario compile` does not bind. The `check_policies_format` text still says `policies.dispatch`.
- **L11:** `max_rounds` is now a hard validation error, and the remaining clamp is untested. Legacy plans with tails are now refused entirely.
- **L12:** the `run_config` meta bytes changed (`COOPERATION.topology`/`market`, `information_model`, top-level `candidate_settings`). Known consumers are fine, but CLAUDE.md §6.27 still names `information_model`. The INFO log line changed and is not pinned.
- **L13:** preflight problems:
  - it ignores per-run overrides;
  - its `ok:false` message is never shown;
  - it costs 1–3 s even with ≤1 structure;
  - a pick made while it is loading can be dropped silently;
  - its reason string ends mid-sentence.
- **L14:** doc nits:
  - the docs cite the scratchpad plan;
  - §6 lists only some of the messages;
  - `CompanyRule` is called "removed" but was moved;
  - the gates are quoted at `f7ece34`, not `44922b4`;
  - the FE row omits the new files.

## Claims the reviewer tried to break and could NOT

- **Default byte identity** (goldens + 515 differential cases + run names).
- **Ownership against an HONEST solver.** All of these are refused:
  - an undeclared pair at the top, through `call`, under Partitioned, or with `narrow`;
  - copied docs, a reused sub-tick, duplicates, `None` hauliers;
  - messages 23 and 24 at both bind and assign.
- **Fail-loud paths:**
  - message 26 fires on every entry point;
  - message 25 has no false positive on the built-ins;
  - the sharing, unknown-`--solver` and `plan`-timing refusals fire everywhere;
  - the dry run matches the sim main's override order.
- **Tests:**
  - the 87 removed ids are renames, ports or deliberate retirements, except M5 and L11;
  - no skips were added and no asserts were loosened.
- **FE:**
  - tsc 0, vitest 632;
  - the shapes match the real CLI output and the stamp;
  - old runs render;
  - no hardcoded solver names.
- **Doc patches:** they apply cleanly, and their paths, signatures and messages are correct.


---

# Appendix B — re-review, round 2 (on 6647aaf), with the round-3 threat model

## Re-review round 2: backend @ 6647aaf, FE @ 6ea517e

Counts: 0 BLOCKER · 2 HIGH · 9 MEDIUM · about 14 LOW. Probes are in `$SP/review_final/r2_{fa,fb,fc,fe,bytes}/` and progress notes in `r2_progress.md`.

Byte identity of default runs HOLDS:
- 383 + 132 differential cases: only the 4 documented G2 cases differ.
- 612 cases through the real `__init__`, multi-tick, with odd values: 0 false violations.
- All 16 specs compile byte-identically.
- Run names and stamps: no difference vs 44922b4.
- Perf vs 44922b4: 0.99–1.04×.

## THREAT MODEL (decision by the orchestrator, round 3)

**In scope:**
- a solver's honest bugs;
- crafted VALUES flowing through the contract (subclassed str/dict, non-JSON, mutation of documents, world or tick state);
- anything that silently corrupts the research metric.

**Out of scope, documented as a limit:**
- interpreter-level tampering: reflection (closure `cell_contents`, `object.__setattr__`, `__class__` swaps, gc), monkeypatching framework classes or modules;
- obfuscated name spellings that evade the static guard (concatenation, `.lower()`, `__module__`).

The `tick.py` docstring and doc 26 must say this explicitly, including that monkeypatches persist in long-lived celery workers.

## HIGH
- **N1** (CONFIRMED). A `str`-subclass pair id defeats the split between check and commit: it hashes as `"o_acme"` during the checks and as `"o_cargo"` at commit.
  - Where: `invariants.py:63` (`isinstance` str), then repeated lookups in `check_result`, `runner.py:193/205` and `tick.py:712`.
  - Probe: `r2_fa/probe_strsub.py`.
  - Fix: `type(x) is str` in Result. The check returns the document pairs it judged, and the commit uses exactly those, with no second lookup.
- **N2** (CONFIRMED). Solver code runs after the integrity scan. Note values are kept as given, so a dict subclass's `__iter__` runs inside `check_wire_notes` (`invariants.py:108,113`), which comes after `changed()` (`runner.py:193` precedes `:199`). It renames `_id` or relabels the carrier, so the commit is retargeted or `shared=False` hides a cross-company haul.
  - Fix: normalise the notes to plain JSON (exact builtin types) immediately after the solve. Make the document scan the LAST step before commit. Build the wire from the normalised copy.

## MEDIUM
- **N3**: market provenance (`pool_id`/`market_round`) is forged through a lying-note subclass. The N2 fix closes it.
- **N4**: instance `meta_key` overwrite. `run_stats.py:170` pushes `agg.meta_key` from the instance, but only the class key was validated. `self.meta_key="policies"` overwrote the stamp. Fix: push under the validated key.
- **N5**: the ceiling can be widened ACROSS ticks.
  - `bind.py:113`: `_WorldState.data` is a shallow copy, so `_WORLD_STATE[tick.world].data["cooperation"]` is the agent's live profile.
  - On tick 1 `solve_tick` appends a structure. On tick 2 the digest changes, the tree is re-bound with the wider ceiling, and an undeclared pair is published as shared with violations=0.
  - Fix: freeze a deep copy at run start, keep the run-start ceiling, and refuse a re-bind that widens it.
- **G1**: any directory named `compat` is invisible to both guards (`tests/_framework_guard.py:84` checks for `"/compat/" in path`). Fix: exempt exactly the `apps/container_logistics/policies/compat/` prefix.
- **G2**: tier coverage stops at the tier directories. A new sibling module, e.g. `assignment/publish_helpers.py`, imported by `app.py` is unscanned. Fix: scan the static import closure of tier A, except compat, solver_lib and the library.
- **G3**: stamp and run-name branches keyed on class or MRO are undetected.
  - B1 (tampered `world_reads` in `policies_stamp`) and B4 (`planner.timing` → `"plan"`) survive the full suite. B2/B3/C2 (an extra run-name segment) fail only incidentally.
  - Fix: store the stamp, `stamp_settings` and run-name segments for every central-top AND golden case, captured on UNMODIFIED code (44922b4 is fine: stamp and run names are byte-identical to 6647aaf). Drive the sim main's own stamp and run-name functions, not reimplementations.
- **G4**: `contract.information_scope` never fails.
  - At `battery.py:1066-1091`, a BadgeLiar ("per company" badge, one restrict over the whole tick, `audit_scopes → []`) PASSes with "saw 4 of 4 hauliers".
  - A leak whose sub-ticks each hold all trucks PASSes too.
  - Fix: FAIL when the measured containment contradicts the badge (for example, a per-company badge on a multi-company tick but some inner call saw more than one carrier). Probes: `r2_fb/probe5/`.
- **M-A**: the run-declaration checks (`_apply_run_declaration`, `run_container_logistics_simulation.py:254-300`: the `stamp_settings` collision and COOPERATION) are skipped by the dry run and by compile. A ClashToy with `stamp_settings={"STREAM_GEO":True}` passes validate/preflight/compile, then every run exits 2 out of sight. Fix: make it a pure check and call it from both.
- **M-C** (perf): the always-on dry run costs about 2.6 s per unflagged run start, because it parses the whole 203 MB consortium bundle (2974 ms at head vs 328 ms at base). The target is 200–500 ms (CLAUDE.md §6.4). Fix: cache the parsed assignment collection by (path, mtime, size), or use a sidecar.

## LOW (fix where cheap)
- **N6**: `tick._shared.remember(t,o)` is callable, so an infeasible pair commits. Move it into a closure.
- **N9**: `RunWorld` refusals raise `TypeError`, not a contract violation, so `dict(tick.world)` aborts every tick. Make it a violation, or keep `TypeError` and document it.
- **M-B**: `ORPHAN_TIMEOUT` and `CONCURRENT_SCHEDULERS` are read by framework code but not reserved. Add them to `FRAMEWORK_RUN_SETTINGS`, or derive the list from the readers.
- **L1 remainder**: a non-PolicyError raised in a solver's `__init__` exits 1 with a traceback, and `validate-run-policies` gives no JSON. Wrap it the same way as bind.
- **L-D** (PLAUSIBLE): an agent refused at init still publishes ready, and the run completes with no assignments and `ok`. orsim's `init_failure_handler='hard'` is a no-op. Make it fail the run from the APP side (`OpenRideAgent`, the scheduler, or a sim-main check). Never edit orsim.
- **Guard gap**: the `candidates` identifier exemption admits `if params.candidates == "spatial":`. Ban comparisons of that attribute with a string literal outside compat.
- **FE**: a solver run-name segment equal to a declared structure id mislabels the arm. The backend should refuse such a segment at bind.
- **Doc**: the valtable delta is 15 `information_scope` rows (GreedyNearest 2, RandomAssignment 2, Partitioned 2, PooledMarket 9), not 12.
- **N8**: fingerprint cost is about 22% at 2 KB documents (about 3% at 194 B). Measure real Eve document sizes and report them. Optimise only if real documents are large.
- **N10**: legacy solvers that write scratch keys into documents now have every tick discarded (no in-repo instance). Document it.
- **LOW-1**: eager agent init refuses mirror-contradicting profiles that bypass the sim main. Add a multi-tick via-init golden if cheap.
- **Out of scope**: N7, G5 and obfuscated spellings (see the threat model). Document them.
