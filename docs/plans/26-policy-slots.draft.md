<!-- In-repo copy of /home/user/docs/reference/features/26-policy-slots.md (the installed, memsearch-indexed copy). Keep the two identical; edit the installed one first. -->

# Policy slots — one whole-tick planner solver behind a generic contract

> Built 2026-10-21 (round 2) and 2026-10-22/23 (the generic solver contract, workstreams
> WS0-WS8 of `docs/plans/policy_slots_contract.md`) on branch `feat/policy-slots` (worktree
> `/home/user/openride_apps_policy_slots`; round-2 HEAD `f8b91b9`, contract base `35b5b92`,
> merges #1-#8 up to `b70c2ec`, WS8 = merge #9, final-review fixes up to `40548a5`) and analytics branch `feat/policy-slots`
> (`IORA-NUS/openroad_viz`; round-2 HEAD `57cba42`, contract frontend = WS6, merged after the
> backend). Plans, each with the coder's Deviations: `docs/plans/policy_slots_p0_p3.md`
> (round 1, P0-P3), `docs/plans/policy_slots_round2_whole_tick.md` (round 2) and the
> contract plan `docs/plans/policy_slots_contract.md` (WS0-WS8; review fixes on top, §7). Authoring guide (worked, battery-checked
> leaf and composite): `docs/policy_authoring.md` in the repo.
> Read with `21-shared-pool-cooperation.md` (the market, now a solver) and
> `11-per-run-solver-override.md` (`--solver`, now "innermost leaf").

**Status: built on branch `feat/policy-slots` (backend) / `feat/policy-slots` (analytics), NOT
merged into the main tree, not live yet.** No run on this box has used this code (the served
dashboard and the celery workers run the main tree). Offline gates: see §7; the live gates are
PENDING (they need the merge + a celery restart). Every existing scenario at its defaults is
meant to be byte-identical (scope in §5). The default planner is
`Partitioned{inner: <the scenario's solver>}`; do NOT change it without an explicit decision
(same rule as doc 21 §9 trap 3).

---

## 1. What was built

Every simulation decision was meant to become a **slot** with registered, typed, validated
**policies**. Only `planner` (which free truck serves which unassigned order) exists.

| Item | Commit(s) | What |
|---|---|---|
| G0 | `277e67b` | Pre-v2 dispatch golden captured on `ec085f5` (`tests/golden/dispatch_golden_v1.json.gz`, 68 cases + 2 meta tests). Never regenerated. |
| P0 | `959b056` | `policies/framework`: slots, typed `PolicyParams`/`param()`, `@policy` registry, catalog. Unknown names fail loud. |
| P1 | `b143087`, `14dfd81` | `DispatchPolicy.decide(ctx) -> Plan`, invariants, discard + log of a violating plan; `RandomAssignment candidates=spatial`. |
| P2 | `b98d452`, `ba79a87` | `spec.json` `policies` block, `ORSIM_POLICY_OVERRIDES`, `run_config.meta.policies` stamp, dashboard Operator policies card, `--policy`/`--policy-param`. |
| P3 | `21bd28e`, `7670b86`, `a075b91` | Contract battery, bench, capture, per-policy golden; `openride policy list/show/validate/bench`. |
| R1 fix | `c30fd25` | A per-run override accounts like a compiled edit; one strict resolver. |
| C0 | `4f01034`, `3d7e84e` | Golden payloads compared modulo the transport's trailing `sim_clock`/`time_step`. |
| C1-C5, C4 | `a7d26a6` ... `572f4e4` | Round 2: `PolicyRef`, components, whole-tick runtime (`Partitioned`, `PooledMarket`), battery per arm, capture /2, `policy_golden_v2.json.gz`, one selection path, `policies_format: 2`. |
| Gate 6, final review | `92950a4`, `76fbcee`, `f2428ab`, `f8b91b9` | GreedyNearest cost closure; review fixes (see the round-2 plan's Deviations). |
| Rename | `b44a586`, `35b5b92` | Slot `dispatch` -> `planner`, no alias. |
| **WS0** | merge #1 | Ownership oracle pins; framework vocabulary + import guards with shrink-only baselines. |
| **WS2** | merge #2 | Legacy translation, mirrors, legacy env and the slot default moved verbatim into `policies/compat/`. |
| **WS5** | merge #3 | Component kinds declare their own order (`register_component_kind(order=)`). |
| **WS3** | merge #4 | Generic per-pair `solver_notes` wire channel (omitted when empty). |
| **WS1** | merge #5 | The contract: `Tick`, `Result`, `bind`/`Declaration`/`RunWorld`, ownership by declaration, `solver_lib/`; `context`/`scopes`/`world`/`allowed`/`compose`/`plan` deleted. |
| **WS4a** | merge #6 | Run stats as a top-declared aggregator; `PooledMarket` bids with its own cost (G2). |
| **WS4b** | merge #7 | `information_model` deleted -> `badge` + stamped `provenance`; stamp `declaration`/`world_reads`; the arm run-name segment from the top's declaration (`COOPERATION` is framework-derived from the world's active arm since the final review, M3); timing `plan` removed. |
| **WS7** | merge #8 | `--structure` fails loud (26), unread world patch refused (25), `run-preflight`, sharing override removed. |
| **WS8** | merge #9 | Compat re-export shims deleted; guards at their end state (zero baselines, exempt set exactly `policies/compat/**`); three litmus tests; docs. |
| C6 / WS6 | analytics | Round-2 FE (`57cba42`); contract FE (badge with `information_model` fallback, `run-preflight` dropdown gating, `--cooperation-structure` dry run) merged after the backend. |

Round 1 let the **framework** choose the scope. Round 2 handed the policy the whole tick. The
contract then removed every solver name and solver concept from the framework: it calls one
solver, gets pairs, and checks them against what the top solver declared.

---

## 2. The boundary

| Question | Owner | Where |
|---|---|---|
| Who owns an order or a truck | **Framework**, read off the document (`constraints.haulier_of`), never a solver input | `dispatch/tick.py` |
| Which cooperation structures exist (the menu) and which arm is active | **World data** (scenario; the Run step's dropdown / `--structure` patch `cooperation.active`) | `profile.cooperation`, passed opaquely as `RunWorld` |
| Which cross-company `(owner, carrier)` pairs a run may commit | **Top solver declares** at bind, inside the **ceiling** = union of every declared structure's adjacency (message 23) | `Declaration.cross_company`, `constraints.declared_partnerships` |
| Every committed pair is own-or-declared AND physically feasible; no truck/order twice; in scope | **Framework** (`check_result`, before commit; sub-ticks too) | `dispatch/invariants.py`, `Tick.permitted` |
| Commit (pairs, in order) | **Framework** | `DispatchRunner.run` |
| `benefit_km`, `owner_reserve_km`, `awarded_cost_km` | **Framework** (from the committed pairs; same pairs, same tags) | `assignment/accounting.py` |
| Wire format of the tags (`v1` / `audited`), `pool_id`/`market_round` routing | **Compat** (name-keyed rules for byte identity) | `policies/compat/wire.py` |
| Run stats (`meta.<key>`) | Top solver declares an aggregator; framework drives and writes it | `Declaration.run_stats`, `dispatch/run_stats.py` |
| Run settings (`COOPERATION`), run-name segments, provenance | Top solver declares; framework merges (refusing a collision) and stamps | `Declaration.stamp_settings` / `run_name_segments` / `provenance` |
| How the tick is split (none / per group / per company), offers, claims, rounds, arbitration | **Solver** | `library/partitioned.py`, `library/pooled_market.py`, components |
| Which truck serves which order inside a (sub-)tick | **Solver** (the leaf) | `assignment/solver/` |

Why: the framework is the part that must not differ between arms (ownership, feasibility,
commit, accounting); "partitioned vs pooled market vs central" is an ordinary solver choice,
and a new solver is one file with zero framework edits.

**Enforced, not asserted.** `tests/test_framework_vocabulary.py` (an `ast` scan) fails if a
framework module (tier A: `policies/framework`, `policies/dispatch/*.py`, battery, bench,
`assignment/app.py`, `assignment/accounting.py`; tier B: `scenario_overrides`, the sim main,
`openride_control/policies.py`, `openride/policy_cmd.py`) names a solver or a solver concept
(`pooled`, `market`, `partitioned`, `greedy`, `topology`, `cooperation`, `pool_id`,
`information_model`, ...). Since WS8 every module must scan to **zero** hits: there are no
baselines, and the only exemption is `policies/compat/**`. `tests/test_framework_imports.py`
forbids tier A to import `solver_lib`, `dispatch/library`, `assignment/solver`, the market
modules, or anything from `compat` outside a reviewed `(module, symbol)` allow-list.
`tests/test_solver_contract_rename_litmus.py` re-registers the four built-ins under fresh
names and replays all 68 golden cases: identical except the documented compat `v1` name rule.

| Policy | Kind | Badge (top) | Share tags | Notes |
|---|---|---|---|---|
| `Partitioned(inner)` | decomposition | `per group` | `v1` (compat name rule) | **Default.** Groups in pre-v2 order; samples from the `random` module (declared `module_rng`) and `order_sensitive`, for pre-v2 byte identity. Reads `cooperation`. |
| `PooledMarket(max_rounds, offer, claim, arbitration, inner)` | decomposition | `per company` | `audited` | The pooled topology as a solver; engine in `assignment/pooled_planner.py`. Declares `run_stats` (`meta.market`). Bids with its own cost (§3.5). |
| `GreedyNearest` | leaf | `central` | `audited` | As `inner`: per scope. At the top: a **central** planner, declaring the active arm's pairs (`PairLeafMixin`). |
| `RandomAssignment` | leaf | `central` | `audited` | Same. Default `inner`. |

`inner` is a `leaf_only` ref and nesting is single-`inner`. **Components** (`market.offer`,
`market.claim`, `market.arbitration`) are `PooledMarket` params, not slots. Slot: `planner`
only (named `dispatch` until 2026-10-22; renamed with NO alias — `policies.dispatch`,
`--policy dispatch=`, `--policy-param dispatch.*`, an `ORSIM_POLICY_OVERRIDES` `dispatch` key
and `openride policy validate dispatch` all fail loud with "(The slot was renamed to
'planner'.)", one table `slots.RENAMED_SLOTS`. NOT renamed: Python identifiers
(`policies/dispatch/`, `DispatchPolicy`, `DispatchRunner`), the legacy profile block
`planner.topology|market|deployment`, `--capture-dispatch` and the
`<datahub>/fixtures/dispatch/<run>` capture dir. Runs stamped before the rename carry
`meta.policies.dispatch`.) The four planned slots (`idle`, `queue`, `world.shedding`,
`world.duration`) were REMOVED (2026-10-22): that behaviour lives in the simulation itself.

---

## 3. Design

### 3.1 Per tick (`AssignmentApp.assign` → `_dispatch_tick`)

1. Read trucks, orders, claimed, busy → `candidates` (unchanged).
2. `runner = self._dispatch_runner(prof)`: the selection bound to the agent's instance
   (`bind_profile` → `BoundTree`), cached on a digest of the world keys the tree read.
3. `tick = runner.tick(...)`: the whole `Tick` (`seed = blake2b(run_id | time_step)`;
   `measure` priced with `self._solver_params`; `execute` = eventlet `tpool.execute` when
   `solve_in_thread`).
4. `runner.run(tick)`: the top's `solve_tick(tick, params)` (through `tick.execute` iff its
   `offload` is `self`; a decomposition is called directly and offloads its own
   `tick.call`s) → capture → `check_result` (message 18) → pairs.
5. `account_shares(pairs, candidates, cost=tick.measure.cost, own_allowed=<strict own rule>)`
   → `render_share_tags(fmt=compat share_tag_format_for(top), solver_notes=result.solver_notes)`.
6. The top declared `run_stats` → `record_tick` folds `result.tick_notes` and patches
   `run_config.meta.<meta_key>` when due (never on a discarded tick). For `PooledMarket` that
   is `meta.market`; a market still committing at `max_rounds` is **truncated** (WARNING per
   tick, `POOLED MARKET TRUNCATED BY BACKSTOP` ERROR once per run,
   `meta.market.ticks_truncated_by_backstop` pushed immediately).
7. One INFO line `Planner <label>: <n> cross-company assignment(s) this tick.`; publish.

Per sub-tick (`tick.restrict` → `tick.call`): the child's params, RNG, tiebreak and rebate book
(its instance's) are bound, the result is checked (message 19 drops that scope), and the pairs
that passed are remembered so the top check re-checks only their ownership half.

**Tick-scope sampling is per haulier**: a central leaf with `candidates=sampled` caps
`max_trucks_per_scope` per company, a decomposition caps each scope whole. Under cooperation a
central arm therefore sees more candidates than `Partitioned` (golden chain tick at cap 40: 160
vs 80 trucks). Stamped as `provenance[planner].sample_scope`.

### 3.2 Selection

`policies.planner` is the only dial; a ref is `{type, params?}` in JSON (`version` too when
compiled/stamped, ignored on read). Compiled profiles carry **`policies_format: 2`** next to
`policies`; a profile with `policies` and without the marker is refused at run start (a
round-1 compile). The marker is checked on the **source** profile before any override resolves.
Profiles without `policies` are legacy and translated (§4).

| Input | Effect |
|---|---|
| `scenario new --solver X` | `Partitioned{inner: X}` (never a flat central `{type: X}`) |
| `scenario edit <slug> --solver X` | the innermost leaf of the current selection becomes X |
| `--solver X` / `ORSIM_SOLVER` (run) | innermost leaf = X, params reset; a decomposition name refused (11); conflict with an explicit `planner.inner.type` refused (12) |
| `--policy planner=GreedyNearest` | a **central** leaf over the whole tick |
| `--policy planner=PooledMarket` | top = PooledMarket; a new decomposition keeps the current per-scope solver as `inner`, unless the request names it |
| `--policy-param planner.inner=R` / `planner.inner.k=v` / `planner.offer.keep_below_km=3` | ref type / nested param |
| `--structure X` / `ORSIM_COOP_STRUCTURE` / the Run step's dropdown | patches `cooperation.active` in memory; an undeclared id or a scenario without a cooperation block is **refused** (message 26); a selection whose `bind` reads no `cooperation` is **refused** (message 25) |
| `--sharing-policy` / `ORSIM_SHARING_POLICY` | **refused** ("removed: inert at run time"; set `planner.sharing` in `spec.json` and recompile) |

One application point, `scenario_overrides.run_dispatch_overrides` (sim main and control dry
run): overrides → legacy env (`compat/legacy_env.py`, handed through opaquely as `legacy_env`)
→ `--solver`. `validate-run-policies --scenario S [--solver] [--policy] [--policy-param]
[--policy-overrides] [--cooperation-structure]` returns `{ok, action: "validate_run_policies",
overrides}` or `{ok: false, action, message}`; with `--cooperation-structure` it applies the
override on a copy and refuses 26/25 synchronously (the dashboard maps `ok:false` → HTTP 400, a
non-answer → 503). `run-preflight --scenario S [...]` always reads the bundle and returns
`{ok, action: "run_preflight", world_reads, structures: {active, declared}}`; the dashboard
disables the structure dropdown, with a reason, when `world_reads` lacks `cooperation`.

**Run-name segments.** Legacy forms byte-identical (` · GreedyNearest` for `--solver`;
` · pooled` / ` · partitioned` for the topology env, same position; market env adds none). Leaf
only touched → ` · Leaf` / ` · Leaf(k=v)`; a higher node touched → the slash chain
` · PooledMarket/GreedyNearest`. The `--structure` path appends ` · <id>` in its old position;
then the top's `run_name_segments` are appended, idempotent by segment. **Every run whose top
solver reads `cooperation` names its arm**: the four built-ins declare the active arm's id when
it has one (exactly the pre-contract condition, so run names are byte-identical). A top that
reads no `cooperation` declares no segment. **Known limit (not changed):** a central
`--policy planner=GreedyNearest` and `--solver GreedyNearest` both read ` · GreedyNearest`. Use
the stamp (`label`, `provenance[planner].badge`).

### 3.3 The stamp

`run_config.meta.policies.planner`: `name`, `version`, `kind` (`operator`), `timing`
(`online`, the only timing), `source` (`bundle` / `legacy-profile` / `override`),
**`label`** (`PooledMarket/GreedyNearest`), `leaf`, `params` (nested; refs
`{type, version, params}`), `share_tag_format` (compat rule), `accounting_params`, plus:

- **`provenance`**: `{<node path>: <that node's Declaration.provenance>}` (`planner`,
  `planner.inner`). The built-ins' top declares `{badge, sample_scope, candidate_settings}`:
  `central` / `per_haulier` for a top leaf, `per group` / `per_group` for `Partitioned`,
  `per company` / `per_company` for `PooledMarket`; an inner declares `candidate_settings`.
- **`declaration`**: `{cross_company: [[owner, carrier], ...]}`, the top's declared pairs,
  sorted.
- **`world_reads`**: the sorted world keys the bound tree's `bind` calls read (the built-ins:
  `cooperation` and the four candidate knobs).

**Gone (WS4b): `information_model` and the top-level `candidate_settings`.** Runs stamped
before this change carry them, and `provenance` does not exist there; the FE falls back to
`information_model` for those runs.

The top's `stamp_settings` are merged into the run settings: the built-ins declare
`COOPERATION = {structure_id, components, adjacency, pools}` (`solver_lib.cooperation.stamp`;
the analytics agent's planner rows read `structure_id`/`components`). Its old `topology` and
`market` keys are gone (the selection and the market config are in `meta.policies.planner`).
A stamp setting that collides with a run setting (`POLICIES`, `STREAM_GEO`, ...) is refused,
exit 2. `meta.market` is written only by `PooledMarket`'s aggregator.

Console: `Policies effective: planner=<label> v<v> timing=<t> params=<json>
accounting_params=<json>` (`information_model=` dropped) and `Run declaration:
cross_company=<n> pairs, world_reads=<keys>`. The pre-contract `Cooperation effective: ...`
and `Cooperation active structure: ...` lines are gone.

### 3.4 Accounting: one computation, two wire formats (compat)

`assignment/accounting.py`: cross-company pairs only, after the commit; the numbers come from
the committed pairs and the tick's measurement, so the same committed pairs get the same tags
whichever solver produced them. They still depend on the result: `owner_reserve_km` is the
owner's cheapest own truck still free **after** the tick's commits (`None` when none),
`awarded_cost_km = float(cost(t, o))` (exception → `inf`), `benefit_km = reserve − awarded`,
**signed**, `None` without a reserve, rounded to 0.001 km.

The rendering lives in `policies/compat/wire.py`:

- **`v1`** (4 keys, a non-finite benefit stays `-inf`; FIX-5 defect) — for exactly a
  `Partitioned` top, an explicit NAME-keyed compat rule (`V1_SHARE_TAG_BUILTINS`); no
  registration can opt in (message 15 retired), because every pre-v2 partitioned run carries
  exactly these bytes.
- **`audited`** (8 keys, non-finite → `None`) — every other top, central leaves included.
- **`solver_notes`**: a solver's per-pair notes. The keys `pool_id` / `market_round` are routed
  by NAME to their old wire fields (5th / 8th audited key); any other key goes to a
  `solver_notes` tag key and `AssignedHaulTripPayload.solver_notes` field, **omitted when
  empty** (no built-in emits one, so no byte changed), which the truck agent copies into the
  trip's `collaboration`. Only SHARED pairs carry tags, so notes on a self-serve are dropped.

`awarded_cost_km` is **recomputed** from the measurement, not read from the winning bid.

### 3.5 The generic contract

What a solver is (full guide: `docs/policy_authoring.md`):

- **Registry entry** (`@policy`): `slot`, `name`, `version`, `description`, a nested `Params`;
  optional `requires`, `order_sensitive`, `module_rng`, `perf_budget_ms`, `arm_budgets_ms`,
  `offload` (`self` | `children`), `badge` / `badge_tooltip` (display only). The class needs a
  callable `solve_tick` (the helper base `DispatchPolicy` supplies one for a legacy
  `decide(ctx)` through `compat.legacy_context`).
- **`bind(params, world, *, top) -> Declaration`**, once per node per process at run start,
  pure. `Declaration`: `cross_company` (top only, ⊆ the ceiling, message 23; an inner ⊆ the
  top's, message 24), `candidate_hint` (read by a caller via `tick.callee(ref)`),
  `provenance`, and the top-only `stamp_settings`, `run_name_segments`, `run_stats`
  (message 24 on a nested node).
- **`RunWorld`**: the profile minus the selection keys, read-tracking. A `bind` reads every
  key its `solve_tick` reads (battery `contract.world_reads_at_bind`); the union is stamped and
  decides message 25.
- **`solve_tick(tick, params) -> Result`**. `Tick`: `raw_trucks`/`raw_orders` (read-only),
  views, `permitted` (own-or-declared AND physical), `serves`, `measure`, `rng` (top
  `Random(seed)` lazy; sub-tick = callee instance's), `sample_rng`, `seed`, `clock`, `world`,
  `candidates`, `scratch`, `execute`, and nesting `restrict` / `call` / `callee` / `docs` /
  `note_incident`. `Result`: `pairs`, `solver_notes`, `tick_notes`.
- **Single `inner`**: one planner ref named `inner` per node.
- **Solver helpers** in `policies/solver_lib/` (candidates, objective, cooperation arm
  resolution / message 14, `PairLeafMixin`, `PairSolver`); the framework never imports them.

**The G2 boundary (WS4a).** `PooledMarket` prices offers, claims, arbitration and award
`cost_km` with its OWN cost, built from its inner's `dual_cycle_bonus_km` /
`dual_cycle_radius_km` when the inner declares them (GreedyNearest) and the constraint defaults
otherwise; it no longer borrows the framework's accounting measurement. Identical wherever the
live dials equal the inner's (the one-dial mirror) or are at the defaults — every shipped bundle
and every golden case. **Off-default boundary: pooled + a cost-blind inner (RandomAssignment) +
non-default `solver_params` now bids with the constraint defaults** (the accounting still
prices with `solver_params`). Moving the dials to a world measurement setting is a later phase.

**Removed:** timing `plan` (held tails; only `online` was ever executable — `slots.py` now
declares `timings=("online",)`); `information_model`; the framework's world/cooperation
objects (`DispatchWorld`, `PartitionRule`, `CompanyRule`, `ScopeDecider`, scope builders); the
`Plan` type outside compat; the reserved notes keys `market` / `pair_notes`.

---

## 4. Legacy translation (`policies/compat/`)

`compat/legacy.py` (translation + echoes), `compat/legacy_profile.py` (a profile with no
`policies` block → resolved policies), `compat/legacy_env.py` (the legacy env vars),
`compat/mirrors.py` (mirrors + message 13). The old re-export paths
(`policies/dispatch/legacy.py`, `dispatch/plan.py`, the re-exports in `dispatch/profile.py` and
`scenario/scenario_overrides.py`) were **deleted in WS8**.

| Legacy input | New selection |
|---|---|
| `solver` / `planner.deployment` / `solverParams`; topology absent or `partitioned` | `Partitioned{inner: {type: S, params: P∩declared}}` |
| same, `planner.topology` `pooled` / `two-stage` | `PooledMarket{max_rounds, offer, claim, arbitration from planner.market (defaults OfferAll / ClaimAllPlanned / LowestCost / 20), inner: S}` |
| bundle profile without `policies` | same; `use_spatial_matching: false` → `inner.candidates = "sampled"`; no `strategy` → `RandomAssignment` |
| undeclared leaf/component params | dropped at WARNING |
| unknown topology (6) / component (3) / `max_rounds` not int in 1..100 | **loud** `PolicyError` / `SpecValidationError` |
| `ORSIM_PLANNER_TOPOLOGY` | top = PooledMarket / Partitioned, inner kept; conflict with explicit `planner.type` (10) |
| `ORSIM_OFFER_POLICY` / `_CLAIM_POLICY` / `_ARBITRATION_RULE` / `_MARKET_MAX_ROUNDS` | component (params reset) / `max_rounds` 1..100; top must be PooledMarket (9) |
| `policies` without `policies_format: 2` | refused at run start: recompile |

Compile writes mirrors through `mirror_dispatch_selection` (bundle bytes unchanged). At run
start the `policies` block is authoritative; `check_mirrors` (13) checks, never reads. Message
14 (pools must pair only adjacent companies) is checked by each built-in top's `bind`
(`solver_lib.cooperation.validate_arm`), i.e. at run start for every built-in.

---

## 5. Comparability

- **Defaults are byte-identical**, scoped exactly: the 68-case pre-v2 golden (pairs, commit
  order, share tags, published payloads modulo the transport's trailing
  `sim_clock`/`time_step`, round stats, module-RNG consumption), the 15-case via-`__init__`
  subset, `policy_golden_v2`, and the out-of-grid comparison over real bundles: **204/204
  identical** (17 compiled bundles × partitioned / pooled / env-pooled × 4 ticks), held at
  every contract merge. Run names are byte-identical on 17 bundles × {default arm, `--structure`
  other arm, `ORSIM_PLANNER_TOPOLOGY=pooled`, `--solver RandomAssignment`}, except that the
  inputs WS7 now refuses (`--structure` on a scenario without a cooperation block — 8 of the
  17 main-tree bundles — and a typo'd structure) EXIT with message 26 instead of running
  silently on the default arm.
- **Selecting the old pooled topology is the same arm, where `planner.market` is the default**
  (unchanged from round 2).
- **New arms, NOT comparable to existing runs:** a top-level leaf (central); `PooledMarket`
  with a different `inner`; `RandomAssignment candidates=spatial`; **any params change**; and
  pooled + cost-blind inner + non-default `solver_params` (G2, §3.5). **G2 is wider than that
  one case (final review M6):** a bundle whose `solver_params` LACKS the dial keys, run with
  `--policy-param planner.inner.dual_cycle_bonus_km=X` (or any other bid dial), also changes
  the market's bids — the old mirror rule only rewrote keys already present, so the old bid
  used the defaults and the new one uses X. No shipped bundle is exposed, but any pooled run
  with a dial override is a new arm. Read the arm from `label`
  + `provenance` (`information_model` on runs stamped before this change), not from the run name.
- Noise is ~4–5% on `empty_ratio` (CLAUDE.md §8): n≥3, interleaved, report `dual_cycle_rate`
  beside it. `policy bench` deadhead is haversine at decision time, not the road KPI.
- **`scripts/broker_gate.sh` (untracked, unchanged) REQUIRES `--structure`** (CLAUDE.md §8).
  Since WS7 it can no longer gate a scenario without a cooperation block: `--structure` is
  refused there (message 26). It also cannot gate a selection whose top reads no
  `cooperation` (message 25). Gate such scenarios another way.

---

## 6. Traps

1. **`--policy planner=GreedyNearest` is a CENTRAL planner.** To change only the matcher use
   `--solver X` or `--policy-param planner.inner=X`. Check `label` / `provenance` badge.
2. **Round-1-compiled bundles are refused** (`policies_format` missing). Recompile; never
   hand-patch.
3. **`v1` tags are a compat NAME rule for a `Partitioned` top** (`compat/wire.py`
   `V1_SHARE_TAG_BUILTINS`), not a registration option. A renamed or subclassed copy of
   `Partitioned` under another name gets `audited` tags (the rename litmus pins exactly this,
   and it is the ONLY name-keyed difference). Do not "clean up" the `-inf` rendering or
   generalise the rule: every pre-v2 partitioned run carries these bytes.
4. **RNG.** The top `tick.rng` is `Random(seed)`, built on first read; a sub-tick's `rng` is
   the CALLEE instance's injection (the `random` module by default), and `sample_rng` is what
   the caller passed to `restrict` (required keyword). `Partitioned` never reads `tick.rng` and
   passes `sample_rng=random` (declared `module_rng`), so no tick `Random` is built on the
   default path. A NEW composite injects `inner.set_rng(tick.rng)` / `set_tiebreak(tick.seed)`
   and passes `sample_rng=tick.rng`, and then passes `no_module_rng` with no declaration.
   `PooledMarket` keeps calling `set_rng`/`set_tiebreak` on its inner (the determinism teeth
   tests patch `set_tiebreak`), so since WS8 `PooledMarket` REFUSES (at construction, i.e. run start) an inner without that
   injection API (a contract-only leaf with no `DispatchPolicy` base) instead of crashing
   mid-tick; `Partitioned` (which does not inject) takes any leaf, and `CompositePolicy`
   forwards an injection only to an inner that offers it (litmus (b) found both;
   `tests/test_contract_only_inner.py`). The battery attributes each module draw to the node
   that made it.
5. **Fail-loud flips** (exit 2 before agents spawn, or a compile error): unknown topology,
   unknown market component, `max_rounds` outside 1..100, a market env var without
   PooledMarket, env conflicts, `--solver` naming a decomposition, mirror disagreement, a pool
   pairing non-adjacent companies (14), an unknown `--solver`, a declaration outside the
   ceiling (23) or a nested node declaring top-only fields (24), an unread world patch (25),
   an unknown/blockless `--structure` (26, used to warn and run the scenario's own arm), a stamp
   setting collision, and a set `ORSIM_SHARING_POLICY` / `--sharing-policy`.
6. **Two discard levels.** Tick-level violation (18) discards the whole tick; sub-tick (19)
   only that scope — inside `PooledMarket` that company for that round. ERROR lines in
   `journalctl --user -u openride-celery`, **not** `sim.log`.
7. **Crashes.** A top solver's, `Partitioned`'s inner's or a custom composite's exception
   propagates out of the tick; ORSim's step handler catches it, the tick commits nothing and the
   agent lives on. A `PooledMarket` inner crash is `PLANNER POLICY CRASHED` (20), counted
   (`tick.note_incident`), that company commits nothing that round. Offer/claim crashes are
   swallowed. The run still finishes `ok: true`. Neither violations nor crashes are stamped.
8. **Per-arm budgets.** `GreedyNearest candidates=sampled` is budgeted 5790 ms, above
   `STEP_SETTLE_SECONDS` (2 s): at 1000×500 that arm makes the assignment agent a straggler.
9. **Adding a solver needs only a `policy_golden_v2` entry** (by design); no framework file
   (`tests/test_solver_contract_dropin.py`). A new arm of a **built-in** must still be
   acknowledged in `_WANT_ARMS`.
10. **Restart rule.** Edits under `apps/container_logistics/policies/**`,
    `assignment/{app,accounting,pooled_planner,arbitration}.py`, `assignment/policy/*`,
    `assignment/solver/*` need `systemctl --user restart openride-celery`. Selecting a solver
    or params never does. No api rebuild (`run_config.meta` is unschema'd).
11. **Deploy order: backend first.** The dashboard needs `components`, `badge`, `run-preflight`
    and `validate-run-policies --cooperation-structure`; the FE falls back to
    `information_model` for old stamps and keeps the dropdown enabled when `world_reads` is
    absent (older backend).
12. **Capture /1 fixtures are refused** (22); `/2` is read via `compat/capture_v2.py`; new
    captures are `/3`. `every=N` counts **dispatch ticks, not sim steps**.
13. **Run stats and tick notes are TOP-only, by declaration.** `Result` has typed fields, and
    only the TOP's `Declaration.run_stats` aggregator reads `tick_notes` (a nested node declaring
    `run_stats` is message 24). The aggregator writes `run_config.meta.<meta_key>`; a `meta_key`
    that is not a plain identifier or that names a framework-written key (`policies` and the
    other `get_run_config_meta` keys, `run_stats.RESERVED_META_KEYS`) is refused at bind. Reusing
    `meta_key="market"` would still overwrite `PooledMarket`'s record shape (doc 21 trap 6), so
    pick your own key. An aggregator that raises is contained: `RUN STATS DISABLED` at ERROR and
    `meta.<key> = {"invalid": true, ...}`, and the pairs are untouched.
14. **SKIP is not PASS.** Read the skip count, not only the verdict.
15. **The tick and `RunWorld` are read-only and CHECKED, not by convention (final review H1/H2).**
    `Tick` has `__slots__` and read-only properties; assigning one is a contract violation (18 at
    the top, 19 in a sub-tick). The framework fingerprints (pickle) every document when
    `Tick.top` is built, commits only from its own index, and re-checks the fingerprints after
    the solve, so a solver that edits or edits-and-restores a document loses the whole tick.
    Results are read ONCE into plain JSON with exact builtin types (a `str`/`dict` subclass in a
    pair id or note cannot change between check and commit), the commit uses exactly the pairs
    the check judged, and the document scan is the last step before commit. Fingerprinting
    costs ~2.3 us per document (4.5-7.1 ms per tick at real document sizes; 2-8% of the solve
    alone, less of the agent's whole tick); the end-to-end perf gate vs 35b5b92 is within noise.
    The ceiling is frozen at run start (`AssignmentApp.__init__`): a later re-bind may never
    widen it.
    `RunWorld` hands out deep copies, and the profile is fingerprinted across every bind, so a
    `bind` that mutates world data is refused (`BindError`). `[]`/`get`/`in` record a read;
    iteration and `len` show keys but record nothing; `keys()`/`items()`/`values()`,
    `dict(world)` and `**world` raise `TypeError`. Framework code serialises via
    `bind.world_snapshot()`. Limit: Python has no real privacy, so `object.__setattr__`, `gc`
    or closure cells can still reach framework state; the checks defend against mistakes and
    ordinary dishonesty, not against deliberate interpreter-level tampering.
16. **`--structure` on a non-reading selection is refused, not ignored** (25). A solver that
    should respond to the Run step's dropdown must read `cooperation` in `bind` (and declare
    the pairs it commits across companies).

17. **Solver notes cannot forge wire fields.** A note key that names a field `publish()` writes
    from the share tag is allowed only if the top declared it in `Declaration.wire_note_keys`
    (`PooledMarket` declares `pool_id`, `market_round`); otherwise the tick is discarded (18). So
    is a note value that is not plain JSON (NaN included).
18. **An inner's `cross_company` declaration NARROWS its sub-tick** to own-company pairs plus the
    declared ones. An empty declaration (every built-in inner) inherits the caller's pairs, so
    built-ins are unchanged.
19. **Message 25 is per top-level world key, and the ceiling is the union of all declared arms.**
    The framework enforces that the solver reads `cooperation`, and that every committed
    cross-company pair is inside the menu of declared structures; it does NOT enforce that the
    solver used the ACTIVE arm. A run named after one arm is only that arm if the solver honours
    `cooperation.active`. The built-ins do; third-party solvers must.
20. **Inherited `ORSIM_*` environment variables are checked at the dry run too** (`sim_environ`).
    A stray `ORSIM_SHARING_POLICY` (or `ORSIM_SOLVER`, `ORSIM_COOP_STRUCTURE`, bad
    `ORSIM_POLICY_OVERRIDES` JSON) in the shell that starts the stack makes EVERY run refuse,
    now visibly at the dry run instead of out of sight in the sim main. Check `env | grep ORSIM_`
    first when every run is refused.
21. **`market.max_rounds` outside 1..100 is a hard validation error** (it used to be clamped),
    and a legacy plan with round tails is refused outright; recompile rather than edit.
22. **`run_config.meta` bytes changed at this boundary** (runs stamped before the contract carry
    `COOPERATION.topology`/`market`, `information_model` and a top-level `candidate_settings`;
    new runs carry `provenance`, `declaration.cross_company`, `world_reads`, and a
    `COOPERATION` the framework derives from the world's active arm). Every known reader
    (analytics, FE) handles both. The INFO "Run declaration:" log line is not a stable
    interface.
23. **Every launch is dry-run** (`validate-run-policies`, no flags needed): bundle format,
    mirrors, bind, messages 23-26, inherited env. The dashboard shows the refusal as a 400 with
    python's message. `scenario compile` runs the same check and will not write a bundle the sim
    main would refuse. The assignment agent binds eagerly at start with the same check, so a
    stale worker or bundle fails ONCE, at launch ("Failed to launch app"), never as a per-tick
    loop. The run-declaration check (`scenario/run_declaration.py::check_run_declaration`:
    reserved run settings, the framework-derived `COOPERATION`, and run-name segments) is ONE
    pure function called by the dry run, compile and the sim main. The reserved-settings set is
    derived from every reader of `orsim_settings` (a test AST-scans them, so it cannot drift).
    The dry run reads the bundle through a sidecar `scenario.run_inputs.json` (gitignored;
    keyed on the bundle's SHA-256 + stat identity, re-hashed when in doubt, never stale):
    ~350-400 ms per dry run on the 203 MB consortium bundle instead of ~2.9 s. The first read
    after a bundle written by anything other than `scenario compile` pays the full parse once.
24. **A solver run-name segment may not impersonate the framework:** a segment equal to a
    declared structure id other than the active arm, a `lifecycle:` tag, a legacy topology
    segment, or a dispatch-label shape (a registered planner name, `Name(...)`, `A/B`) is
    refused at bind. Compare/the dashboard read the structure from the `COOPERATION` stamp.
25. **Agent init failures.** An `OpenRideAgent` whose app raises at init answers the boot
    handshake with `init_error`, and the scheduler fails a `hard` run at step 0
    (`AgentInitFailure`). A stock agent refused at init (it says `ready`, then never steps) is
    only a HEURISTIC signal: `AGENT NEVER STEPPED` at ERROR, logged once, never fatal, because a
    healthy agent can be pruned during a slow boot on a big live run. Promote it to fatal only
    after a live 500/1000-truck run shows it never fires on a healthy run. Grep the sim log for
    it when a run finishes with no assignments.

---

## 7. Verification evidence

**Round 2, offline, at `f8b91b9`:** golden 94 passed (70 + 15 + 9); out-of-grid 204/204 vs
`277e67b`; full suite matched its baseline; 16/16 specs compile identically; validate
`Partitioned` 47 (3 SKIP), `PooledMarket` 187 (8 SKIP), `GreedyNearest` 47, `RandomAssignment`
47 (2 SKIP); Gate 6 perf partitioned 1.012, sampled 1.021, pooled 1.069 (WAIVED, GC-phase
noise, independently re-measured 1.012 with GC held level; ~+2% steady-state residual); FE
`tsc` 0, vitest 601/601.

**Contract (WS0-WS8), offline, per merge** (each WS ran G-gold, G-oog, G-val, G-full, G-vocab;
G-perf on WS1/WS4a by the orchestrator):
- G-gold: the 4 golden files **101 passed**, and the golden inputs (`dispatch_golden_v1`,
  `policy_golden_v2`, `harness.py`, `compare.py`) byte-unchanged vs `35b5b92`.
- G-oog: **204/204** identical vs the `35b5b92` baseline (profiles regenerated and `cmp`-equal
  on WS2/WS8).
- G-val (current): `GreedyNearest` 50 PASS + 2 SKIP, `RandomAssignment` 48 + 4,
  `Partitioned` 49 + 3, `PooledMarket` 205 + 8, all exit 0. The delta from round 2 is WS4b's:
  `information.*` checks gone, `contract.{scope_audit,bind_pure,world_reads_at_bind,
  declaration_within_ceiling}` added; the only new built-in SKIP is `contract.scope_audit` on
  the two leaves.
- G-full: no new failure vs the `35b5b92` failing set (the remaining failures are outside policy
  code: ride-hail, interaction equivalence, bundle-dependent tests).
- Stamp/run-name oracle (`stamp_harness2.py`, 11 input paths × {compiled, legacy}) and the
  17-bundle run-name harness: stamps differ only by the documented WS4b algebra
  (`information_model` / top-level `candidate_settings` out; `provenance`, `declaration`,
  `world_reads` in; `COOPERATION` minus `topology`/`market`); run names byte-identical; WS7's
  refusals are the expected EXITs.
- Litmus (WS8): (a) a toy composite + leaf through `AssignmentApp.assign`/`publish` — notes on
  shared pairs only, its own `meta.litmus`, provenance and `world_reads` stamped, messages
  23/24/25; (b) a real file dropped into `library/` validates via `python -m openride policy
  validate` and appears as `Partitioned`'s `inner=` arm; (c) the four built-ins renamed replay
  all 68 golden cases identically except the compat `v1` rule.
- WS8 head `f7ece34` (branch `ws/ws8`, before merge #9): G-gold 101 passed + golden inputs unchanged; G-oog 204/204 with the profiles regenerated and `cmp`-equal; G-val as above, per-check table identical to WS4b's; G-vocab zero hits, no baseline directory; stamps and run names byte-identical to merge #8 (`b70c2ec`); G-full 3102 passed / 37 failed / 9 errors / 7 skipped, the only new failing line being the worktree-name artefact `tests/test_ride_hail_event_constants.py`, the only new skips the two bundle pins of `test_ownership_oracle_pins` (92 passed with `ORSIM_SCENARIOS_DIR=/home/user/openride_apps/scenarios`). Re-run on the merge commit.

- **Final review loop (2026-10-23).** An independent adversarial review of the whole branch
  (round 1: 0 blocker / 3 high / 15 medium / 14 low), four parallel fix branches, a full
  re-review (round 2: 0 blocker / 2 high / 9 medium / ~14 low; default byte-identity re-proven
  on 383+132 differential cases, 612 multi-tick `__init__` cases with crafted values, all 16
  specs compiling identically), and a round-3 fix pass under an explicit threat model (§6
  trap 15). **Final head `40548a5`** (branch `feat/policy-slots`; FE `6ea517e`): G-gold 101 +
  golden inputs unchanged; G-oog 204/204 with profiles `cmp`-equal; stamp and run-name
  harnesses byte-identical to 44922b4/6647aaf; guard + integrity + contract tests 934 passed;
  ownership pins 92 (with `ORSIM_SCENARIOS_DIR`); G-full 3833 passed with no new failure;
  validate GreedyNearest 54 (4 SKIP), RandomAssignment 54 (6), Partitioned 54 (3),
  PooledMarket 222 (8), 0 FAIL — the delta vs WS4b is 15 added `contract.information_scope`
  rows; perf vs 35b5b92 partitioned 1.009, pooled 1.047, sampled 0.987 (inside the round-2
  pooled waiver). FE tsc 0, vitest 653/653. Review findings are summarised in
  `docs/plans/policy_slots_contract.md` (appendix).

**Live — PENDING (needs merge + celery restart):**
- [ ] `multi_haulier_greedy_solver_500_trucks` n=3 interleaved: `empty_ratio`,
      `dual_cycle_rate` in band, 100% `osrm`, 0 `PLANNER POLICY` lines, stamp `label`,
      `provenance`, `declaration`, `world_reads` present, run name unchanged, `Run declaration:`
      line printed.
- [ ] `collab_scale_500t_v2 --structure chain --policy planner=PooledMarket` n=3: as above,
      plus `meta.market` present, `COOPERATION.{structure_id,components,adjacency,pools}`
      stamped, analytics planner rows built, and `benefit == reserve − awarded` on every tag
      carrying all three.
- [ ] One `ORSIM_PLANNER_TOPOLOGY=pooled` run whose stamp equals the `--policy` arm.
- [ ] A `--structure typo` and a `--structure` on a blockless scenario: exit 2, message 26,
      and HTTP 400 from the dashboard.
- [ ] Browser: badges in the Policy step; the dropdown enabled on a cooperative scenario; the
      "Checking…" state and a preflight refusal note; a stale bundle's Run shows python's
      message; Compare shows the stamped structure, not a date.
- [ ] `AGENT NEVER STEPPED` never fires on a healthy 500/1000-truck run (then it may be made
      fatal).
- [ ] Dry-run latency on the dashboard Run step with the sidecar warm (target ≤500 ms).
- [ ] `--capture-dispatch` run (format `/3`), then `policy bench --fixtures` over it.

---

## 8. NOT done

- **NetworkFlow** (a central leaf; `requires=("ortools",)`, not in the venv). Selection,
  accounting, stamp and battery come free because it is a leaf. It still needs `tick.travel`,
  facility ids and time windows; tours become pairs in its wrapper (tails are not a framework
  concept), and day-ahead planning would be a separate design (the `plan` timing was removed).
- **Dials → world measurement setting** (changes compile output and `benefit_km`'s response to
  a dial override): a separate decision. Until then the "first node wins" dial rule stays in
  `compat/measurement.py`, and G2's off-default boundary applies (§3.5).
- Stopping the `planner.topology`/`planner.market` echoes in bundles; removing the legacy
  translation once the v1 golden is re-captured against `policies` blocks; `v1` → `audited`
  tag unification (a wire boundary).
- Lifting single-`inner` nesting (`inner_by_company` needs a map-of-refs param type). The
  cooperation **structure** stays world data.
- A declaration digest cross-check between the sim main and the agent (both bind; purity is
  tested, not cross-checked at run time).
- The ` · central:<Leaf>` run-name prefix: proposed, not implemented.
- Violations/crashes are not stamped into `run_config`.
- Compare still reads the arm from the run NAME, not `COOPERATION.structure_id` (doc 21 §2a).
- The live gates in §7.

## 9. Files

| Where | Responsibility |
|---|---|
| `policies/framework/` | `slots.py` (1 slot, `online`), `params.py`, `registry.py` (`@policy`, discovery of `assignment/solver` + `dispatch/library`, `isolated_registry`), `resolve.py` (merge, CLI parse, `policies_stamp`), `catalog.py` (`badge`, `badge_tooltip`, `offload`), `components.py`, `errors.py` |
| `policies/dispatch/` | `tick.py` (`Tick`, `Measure`, `DispatchClock`), `result.py` (`Result`), `bind.py` (`RunWorld`, `Declaration`, `BoundTree`, messages 23-25), `runner.py`, `invariants.py` (`check_result`), `run_stats.py`, `profile.py`, `resolved.py`, `base.py` (`DispatchPolicy`), `composite.py`, `views.py`, `capture.py` (/3) |
| `policies/dispatch/library/` | `partitioned.py`, `pooled_market.py`, `_market_stats.py` (`RoundStatsAggregator`) |
| `policies/solver_lib/` | `candidates.py`, `objective.py`, `cooperation.py` (arm, message 14, `COOPERATION` stamp), `pair_leaf.py`, `pair_solver.py`, `provenance.py` — never imported by the framework |
| `policies/compat/` | the only place framework-side vocabulary may name solvers: `legacy.py`, `legacy_profile.py`, `legacy_env.py`, `mirrors.py`, `defaults.py`, `wire.py`, `measurement.py`, `selection_keys.py`, `unregistered.py`, `legacy_context.py` (`LegacyDecideContext`, `Plan`), `run_stats.py` (`round_stats()` for the frozen golden harness), `capture_v2.py` |
| `policies/testing/` | `fixtures.py`, `battery.py` (`contract.*` checks, arms, budgets, `Skip`), `bench.py` |
| `assignment/accounting.py` | `ShareAccount`, `owner_reserve_km`, `account_shares`, `render_share_tags` (dispatches to compat) |
| `assignment/pooled_planner.py` | market engine only |
| `assignment/policy/{offer,claim}.py`, `assignment/arbitration.py` | components (`register_component_kind(order=)`) |
| `assignment/solver/` | the two leaves |
| `assignment/app.py` | `_dispatch_runner`, `_dispatch_tick` (generic) |
| `scenario/scenario_overrides.py` | `run_dispatch_overrides`, `dispatch_override_label`, `apply_cooperation_override` (message 26) |
| `openride_control/policies.py`, `openride/policy_cmd.py`, `openride/scenario_cmd.py` | dry run / `run_preflight` / validate / bench; CLI |
| `tests/` | `test_framework_vocabulary.py`, `test_framework_imports.py`, `test_ownership_oracle_pins.py`, `test_solver_contract_{types,litmus,dropin,rename_litmus}.py`, `test_run_stats_contract.py`, `policy_registry_names.py`, `golden/` (`dispatch_golden_v1.json.gz` untouched) |
| FE | `lib/policy-slots.ts` (`policyBadge`), `lib/policy-catalog-types.ts`, `components/scenario/{PolicyParamsForm,PolicySlotsEditor,SimulationTab}.tsx`, `app/api/scenario/run-preflight/route.ts`, `app/api/scenario/run/route.ts`, `lib/compareRunMeta.ts`, `lib/runNameMeta.ts` |
