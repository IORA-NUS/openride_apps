<!-- In-repo copy of /home/user/docs/reference/features/26-policy-slots.md (the installed, memsearch-indexed copy). Keep the two identical; edit the installed one first. -->

# Policy slots — one whole-tick dispatch solver; decomposition is a solver choice

> Built 2026-10-21 (commit dates) on branch `feat/policy-slots` (worktree
> `/home/user/openride_apps_policy_slots`, backend HEAD `f8b91b9`) and analytics branch
> `feat/policy-slots` (`IORA-NUS/openroad_viz`, HEAD `57cba42`). Plans, each with the coder's
> Deviations: `docs/plans/policy_slots_p0_p3.md` (round 1, P0-P3) and
> `docs/plans/policy_slots_round2_whole_tick.md` (round 2). Authoring guide (with a worked,
> validated leaf and composite): `docs/policy_authoring.md` in the repo.
> Read with `21-shared-pool-cooperation.md` (the market, now a solver) and
> `11-per-run-solver-override.md` (`--solver`, now "innermost leaf").

**Status: built on branch `feat/policy-slots` (backend) / `feat/policy-slots` (analytics), NOT
merged into the main tree, not live yet.** No run on this box has used this code (the served
dashboard and the celery workers run the main tree). Offline gates: see §7; the live gates are
PENDING (they need the merge + a celery restart). Every existing scenario at its defaults is
meant to be byte-identical (scope in §5). The default dispatch is
`Partitioned{inner: <the scenario's solver>}`; do NOT change it without an explicit decision
(same rule as doc 21 §9 trap 3).

---

## 1. What was built

Every simulation decision is meant to become a **slot** with registered, typed, validated
**policies**. Only `dispatch` (which free truck serves which unassigned order) is implemented.

| Item | Commit(s) | What |
|---|---|---|
| G0 | `277e67b` | Pre-v2 dispatch golden captured on `ec085f5` (`tests/golden/dispatch_golden_v1.json.gz`, 70 cases). Never regenerated. |
| P0 | `959b056` | `policies/framework`: slots, typed `PolicyParams`/`param()`, `@policy` registry, catalog. Unknown dispatch names fail loud. |
| P1 | `b143087`, `14dfd81` | `DispatchPolicy.decide(ctx) -> Plan`, invariants, discard + log of a violating plan; `RandomAssignment candidates=spatial`. |
| P2 | `b98d452`, `ba79a87` | `spec.json` `policies` block, `ORSIM_POLICY_OVERRIDES`, `run_config.meta.policies` stamp, dashboard Operator policies card, `--policy`/`--policy-param`. |
| P3 | `21bd28e`, `7670b86`, `a075b91` | Contract battery, bench, capture, per-policy golden; `openride policy list/show/validate/bench`. |
| R1 fix | `c30fd25` | A per-run override accounts like a compiled edit; one strict resolver. |
| C0 | `4f01034`, `3d7e84e` | Golden payloads compared modulo the transport's trailing `sim_clock`/`time_step`. |
| C1 | `a7d26a6`, `1047f9c` | `PolicyRef`/`ref()`/nullable numbers; `framework/components.py`; `@policy(information_model=, share_tag_format=, module_rng=, arm_budgets_ms=)`; 5 slots, dispatch default `Partitioned`. |
| C2 | `ba74fbe` | `dispatch/legacy.py`, `dispatch/world.py`, `assignment/accounting.py`, resolution tree, `bind_instance`, mirrors. |
| C3 | `d87f8f6`, `d45741b` | Whole-tick runtime: `Partitioned`, `PooledMarket`; `_assign_pooled`, `_market_components`, `_resolved_topology` deleted from `assignment/app.py`. |
| C5 | `2a1ccf2` | Whole-tick battery, per-arm budgets, capture /2, bench columns, `policy_golden_v2.json.gz`. |
| C4 | `fb14a88`, `572f4e4` | One selection path (`scenario_overrides.run_dispatch_overrides`) for spec, env, CLI and control; `policies_format: 2` run-start check. |
| Gate 6 | `92950a4` | GreedyNearest's per-pair cost chain cut to one closure (`objective.dual_cycle_cost_fn`); `constraints.assignment_cost` inlines the dual-cycle predicate again. Behaviour-preserving (golden 94/94). |
| Final review | `76fbcee`, `f2428ab`, `f8b91b9` | `partition_context(sample_rng=)` required; `PooledMarket` company solves through `ctx.execute`; source-profile marker check before overrides; name grammar; capture counts ticks; compile writes only authored slots; battery SKIP status, scoped `module_rng`, derived inner-arm budgets, registry-derived test name lists. |
| C6 | analytics `03607b9`, `178caca`, `d7650f9`, `57cba42` | Synchronous `validate-run-policies` (400/503), recursive `PolicyParamsForm`, info-model badges, `components` from the policies route, run-name/stamp classification in Compare. |
| C7 | (this doc) | Docs. |

Round 1 let the **framework** choose the scope (one call per partition or per company, two
topologies hard-wired in `AssignmentApp`). Round 2 hands the policy the whole tick.

---

## 2. The boundary

| Question | Owner |
|---|---|
| Who owns an order (`haulier_of`, immutable) | **Framework** |
| Which cooperation structure / pools exist; who may serve whom (`ctx.world.allowed`) | **Framework** (world data; the Run step's structure dropdown) |
| Every committed head is world-allowed; no truck/order twice | **Framework** (tick-level `check_plan`) |
| Committing chain heads (`online`: heads only) | **Framework** |
| `benefit_km`, `owner_reserve_km`, `awarded_cost_km` | **Framework** (`assignment/accounting.py`; computed from the committed pairs, so the same pairs get the same tags whichever solver produced them) |
| `meta.market` patch | **Framework**, only when the plan's notes carry `market` |
| How the tick is split (none / per group / per company) | **Solver** |
| Offers, claims, rounds, arbitration, I-P1..I-P4 | **Solver** (`PooledMarket` + components) |
| Which truck serves which order inside a scope | **Solver** (the leaf) |

Why: the framework is the part that must not differ between arms (feasibility, commit,
accounting); "partitioned vs pooled market vs central" is then an ordinary policy choice, and a
central planner (NetworkFlow, P4) is just a leaf.

| Policy | Kind | Information model | Share tags | Notes |
|---|---|---|---|---|
| `Partitioned(inner)` | decomposition | `per_group` | `v1` (reserved) | **Default.** Groups in pre-v2 order; passes `sample_rng=random` and declares `module_rng` for that draw, plus `order_sensitive` (pre-v2 byte identity). Inner exceptions propagate. |
| `PooledMarket(max_rounds, offer, claim, arbitration, inner)` | decomposition | `per_company` | `audited` | The pooled topology as a solver; engine stays in `assignment/pooled_planner.py`. Each company solve goes through `ctx.execute` (offload depth 1), so `solve_in_thread` (off by default) covers a heavy inner such as NetworkFlow. |
| `GreedyNearest` | leaf | `central` | `audited` | As `inner`: per scope. At top level: a **central** planner. |
| `RandomAssignment` | leaf | `central` | `audited` | Same. Default `inner`. |

`inner` is a `leaf_only` ref, so decompositions do not nest and the effective information model
is the top policy's. **Components** (`market.offer`, `market.claim`, `market.arbitration`) are no
longer slots: `PooledMarket` params, validated against the live `OFFER_REGISTRY` /
`CLAIM_REGISTRY` / `ARBITRATION_REGISTRY`, each with typed `Params`, `version="1"`.
`OfferSpare.keep_below_km` is a nullable number (`None` = the class's `inf`). Slots:
`dispatch`, `idle`, `queue`, `world.shedding`, `world.duration` (last four planned, P5).

---

## 3. Design

### 3.1 Per tick (`AssignmentApp.assign` → `_dispatch_tick`)

1. Read trucks, orders, claimed, busy → `candidates` (unchanged).
2. `world = DispatchWorld.from_profile(prof, accounting_params=self._solver_params)`.
3. `runner = self._dispatch_runner(prof)`: the selection tree bound to the agent's instance
   (`bind_profile_instance`), cached on the profile keys it reads.
4. `ctx = runner.tick_context(...)`, `tick_seed = blake2b(run_id | time_step)`;
   `execute` = eventlet `tpool.execute` when `solve_in_thread`.
5. `runner.run(ctx)`: `decide` (a top-level **non-composite** through `ctx.execute`; a composite
   directly, it offloads its own scopes: `Partitioned` per group, `PooledMarket` per company
   solve; depth is always 1) → capture → `check_plan` vs `world.allowed` → heads.
6. `account_shares(pairs, candidates=, cost=world.accounting_cost, own_allowed=world.own_allowed)`
   → `render_share_tags(fmt=<top's share_tag_format>, pair_notes=notes["pair_notes"])`.
7. `notes["market"]` present → `_record_round_stats` + `meta.market` patch. A market still
   committing when `max_rounds` is reached stops unconverged: the tick is **truncated**
   (WARNING per tick, `POOLED MARKET TRUNCATED BY BACKSTOP` ERROR once per run,
   `meta.market.ticks_truncated_by_backstop` pushed immediately).

**Tick-scope sampling is per haulier** (C3 deviation): a central leaf with
`candidates=sampled` caps `max_trucks_per_scope` per company, a decomposition caps each scope
whole. Under cooperation a central arm therefore sees more candidates than `Partitioned` (golden
chain tick at cap 40: 160 vs 80 trucks). Stamped as `candidate_settings.sample_scope`.

### 3.2 Selection

`policies.dispatch` is the only dial; a ref is `{type, params?}` in JSON (`version` too when
compiled/stamped, ignored on read). Compiled profiles carry **`policies_format: 2`** next to
`policies`; a profile with `policies` and without the marker is refused at run start (it is a
round-1 compile, where a flat `{type: GreedyNearest}` meant the per-scope solver and now means a
central one). The marker is checked on the **source** profile before any override resolves
(`scenario_overrides._profiles_with_current`, with `_commit` as a backstop), so no override
(`--solver`, `--policy`, `--policy-param`, the env vars) can manufacture it. Profiles without
`policies` are legacy and translated.

| Input | Effect |
|---|---|
| `scenario new --solver X` | `Partitioned{inner: X}` (never a flat central `{type: X}`) |
| `scenario edit <slug> --solver X` | the innermost leaf of the current selection becomes X |
| `--solver X` / `ORSIM_SOLVER` (run) | innermost leaf = X, params reset; a decomposition name refused (message 11); conflict with an explicit `dispatch.inner.type` refused (12, naming `--policy/--policy-param/--policy-overrides`) |
| `--policy dispatch=GreedyNearest` | a **central** leaf over the whole tick |
| `--policy dispatch=PooledMarket` | top = PooledMarket; a type change resets params, except that a new decomposition keeps the current per-scope solver (current `inner`, or a leaf top) as `inner`, with its params, unless the request names the inner's type (spec §3.3) |
| `--policy-param dispatch.inner=R` / `dispatch.inner.k=v` / `dispatch.offer.keep_below_km=3` | ref type / nested param |

One application point, `scenario_overrides.run_dispatch_overrides` (sim main and control dry
run): overrides → `ORSIM_PLANNER_TOPOLOGY` → market env → `--solver`. The control plane ships
`--policy*` as `ORSIM_POLICY_OVERRIDES` and `--solver` separately as `ORSIM_SOLVER`.
`validate-run-policies --scenario S [--solver] [--policy] [--policy-param] [--policy-overrides]`
returns `{ok, action: "validate_run_policies", overrides}` or `{ok: false, action, message}`;
names are checked before the bundle is read. The dashboard run route calls it synchronously and
maps `ok:false` → **HTTP 400**, a non-answer → 503.

**Run-name segment** (`dispatch_override_label`): legacy forms byte-identical (` · GreedyNearest`
for `--solver`; ` · pooled` / ` · partitioned` for the topology env, same position; market env
adds none). Leaf only touched → ` · Leaf` / ` · Leaf(k=v)`; a higher node touched → the slash
chain ` · PooledMarket/GreedyNearest`, ` · PooledMarket(max_rounds=5)/GreedyNearest`.
Label values are compact JSON with `ensure_ascii=True` (escaped, never raw ` · `).
Policy and component names must match `[A-Za-z_][A-Za-z0-9_.-]*` (`registry.check_policy_name`,
at registration and on component lookup), so no name contains a separator, `=`, `,` or `:`.
**Known limit (documented, not changed):** a central `--policy dispatch=GreedyNearest` and
`--solver GreedyNearest` both read ` · GreedyNearest`. Use the stamp (`label` +
`information_model`). The proposed ` · central:<Leaf>` prefix is **not implemented**.

### 3.3 The stamp

`run_config.meta.policies.dispatch`: `name`, `version`, `kind` (`operator`), `timing`, `source`
(`bundle` / `legacy-profile` / `override`), **`label`** (`PooledMarket/GreedyNearest`), `leaf`,
**`information_model`**, `share_tag_format`, `params` (nested; refs `{type, version, params}`),
`candidate_settings` (+ `sample_scope` `per_haulier` | `per_group` | `per_company`),
`accounting_params`. Console: `Policies effective: dispatch=<label> v<v> timing=<t>
information_model=<m> params=<json> accounting_params=<json>`. `meta.market` is written only on
the pooled path. The information model is enforced structurally (sub-scopes built by
`scopes.py`) and audited by `information.scopes_respect_model`; a tripwire, not a proof.
The model describes what the **inner leaf** is handed: the decomposition itself sees the whole
tick, and inside `PooledMarket` `claim` gets the market object and arbitration sees every bid.

### 3.4 Accounting: one computation, two wire formats

`assignment/accounting.py`: cross-company pairs only, after the commit. The only solver-supplied
values are `pool_id`/`market_round` from `pair_notes`; the numbers come from the committed pairs
and the world cost, so the same committed pairs get the same tags whichever solver produced
them. They still depend on the plan: `owner_reserve_km` (moved verbatim from `pooled_planner`)
is the owner's cheapest own truck still free **after** the tick's commits (`None` when it had
none), `awarded_cost_km = float(accounting_cost(t, o))` (exception → `inf`), and `benefit_km =
reserve − awarded` is **signed**, `None` when there is no reserve, rounded to 0.001 km.
- **`v1`** (4 keys, a non-finite benefit stays `-inf`, the only reachable one since the reserve
  is always finite; FIX-5 defect) — reserved to `Partitioned` by
  registration (message 15), because every pre-v2 partitioned run carries exactly these bytes.
- **`audited`** (8 keys, non-finite → `None`) — every other policy, central leaves included
  (`pool_id`/`market_round` `None`).

`awarded_cost_km` is **recomputed** from the accounting cost, not read from the winning bid.
Equal today (every claim bids `float(cost)`).

---

## 4. Legacy translation (`policies/dispatch/legacy.py`, one function)

| Legacy input | New selection |
|---|---|
| `solver` / `planner.deployment` / `solverParams`; topology absent or `partitioned` | `Partitioned{inner: {type: S, params: P∩declared}}` |
| same, `planner.topology` `pooled` / `two-stage` | `PooledMarket{max_rounds, offer, claim, arbitration from planner.market (defaults OfferAll / ClaimAllPlanned / LowestCost / 20), inner: S}` |
| bundle profile without `policies` | same; `use_spatial_matching: false` → `inner.candidates = "sampled"`; no `strategy` → `RandomAssignment` |
| undeclared leaf/component params | dropped at WARNING |
| unknown topology (6) / component (3) / `max_rounds` not int in 1..100 | **loud** `PolicyError` / `SpecValidationError` |
| `ORSIM_PLANNER_TOPOLOGY` | top = PooledMarket / Partitioned, inner kept; market from current PooledMarket else `profile.planner.market`; conflict with explicit `dispatch.type` (10) |
| `ORSIM_OFFER_POLICY` / `_CLAIM_POLICY` / `_ARBITRATION_RULE` / `_MARKET_MAX_ROUNDS` | component (params reset) / `max_rounds` 1..100; top must be PooledMarket after the topology step (9) |
| `policies` without `policies_format: 2` | refused at run start: recompile |

Compile writes mirrors through `mirror_dispatch_selection` (`strategy` = leaf; existing
`solver_params`/`deployment` keys; `planner.topology` = echo, removed when none; `planner.market`
= PooledMarket params). At run start the `policies` block is authoritative; `check_mirrors`
(message 13) and `validate_cooperation_world` (14) check, never read. Compile writes only the
`policies` slots the input spec authored back into `spec.json`: a legacy spec keeps its
`solver`/`planner` keys and gains no derived block (the bundle's `$.recipe` still carries the
full block). The pools warning reads the resolved `policies.dispatch` top.

---

## 5. Comparability

- **Defaults are byte-identical**, scoped exactly: the 70-case pre-v2 golden (pairs, commit
  order, share tags, published payloads modulo the transport's trailing `sim_clock`/`time_step`,
  round stats, module-RNG consumption), the 15-case via-`__init__` subset, and the out-of-grid
  comparison over real bundles: **204/204 identical**, `277e67b` vs `f8b91b9` (17 compiled
  bundles × partitioned / pooled / env-pooled × 4 ticks). Runs at defaults before and after
  are comparable.
- **Selecting the old pooled topology is the same arm, where `planner.market` is the default**:
  `ORSIM_PLANNER_TOPOLOGY=pooled` takes the market from `planner.market` and equals the old
  pooled path (tested); `--policy dispatch=PooledMarket` resets the components to their
  defaults. Both give label `PooledMarket/<leaf>`, and they are equal only where
  `planner.market` is the default, which holds for every shipped spec (the `--policy` arm's
  equality is the pending live gate in §7).
- **New arms, NOT comparable to existing runs:** a top-level `GreedyNearest` /
  `RandomAssignment` / any leaf (central); `PooledMarket` with a different `inner`;
  `RandomAssignment candidates=spatial`; **any params change** (incl. `OfferSpare` /
  `ClaimIfGainExceeds` parameterisations and a binding `max_rounds`). Read the arm from
  `label` + `information_model`, not from the run name (§3.2 known limit).
- Noise is ~4–5% on `empty_ratio` (CLAUDE.md §8): n≥3, interleaved, report `dual_cycle_rate`
  beside it. `policy bench` deadhead is haversine at decision time, not the road KPI.

---

## 6. Traps

1. **`--policy dispatch=GreedyNearest` is a CENTRAL planner.** In round 1 it meant
   "GreedyNearest per partition". To change only the matcher use `--solver X` or
   `--policy-param dispatch.inner=X`. Check `label`.
2. **Round-1-compiled bundles are refused** (`policies_format` missing). Recompile
   (`openride scenario compile <slug>`); never hand-patch.
3. **`v1` tags are reserved to `Partitioned`.** Do not "clean up" the `-inf` rendering.
4. **Module RNG.** `Partitioned` passes the `random` module as its groups' RNG and as
   `partition_context`'s `sample_rng` (declared `module_rng`) and never reads `ctx.rng`, so no
   tick `Random` is built on the default path. `sample_rng` is a **required** keyword: a NEW
   composite passes `sample_rng=ctx.rng` and `inner.set_rng(ctx.rng)` and then passes
   `no_module_rng` with no declaration. The battery attributes each module draw to the node
   that made it, so `Partitioned`'s declaration no longer hides an inner's draw
   (`Partitioned[inner=ModuleRandom]` FAILS).
5. **Fail-loud flips** (refuse a run, exit 2 before agents spawn, or a compile error; an
   unknown component also raises inside `assign`): unknown topology (used to fall back to
   `partitioned`); unknown market component (doc 21 §7's "a typo must degrade a run" is
   superseded); `max_rounds` outside 1..100 (used to be clamped; `ORSIM_MARKET_MAX_ROUNDS`
   11..100 now honoured, used to be ignored); a market env var without PooledMarket; env
   conflicts; `--solver` naming a decomposition; mirror disagreement; a pool pairing
   non-adjacent companies; an unknown `--solver` (used to be warned and ignored).
6. **Two discard levels.** Tick-level violation (message 18) discards the whole tick;
   scope-level (19) only that scope — inside `PooledMarket` that company for that round.
   ERROR lines in `journalctl --user -u openride-celery`, **not** `sim.log`.
7. **Crashes.** A top-level leaf's, `Partitioned`'s inner's, a custom composite's or a
   `PooledMarket` arbitration rule's exception propagates out of the tick; ORSim's step
   handler catches it (`Exception in step for Agent …`), the tick commits nothing and the
   agent lives on. A `PooledMarket` inner crash is logged `DISPATCH POLICY CRASHED` (20),
   counted, and that company commits nothing that round. An offer or claim crash is swallowed
   (`Offer/Claim policy failed for haulier …`: that company contributes / bids nothing), not
   counted and not message 20. The run still finishes `ok: true`. Neither violations nor crashes are stamped in `run_config`.
8. **Per-arm budgets.** `GreedyNearest candidates=sampled` is budgeted 5790 ms (measured
   ~1.9 s), above `STEP_SETTLE_SECONDS` (2 s): at 1000×500 that arm makes the assignment agent
   a per-step straggler. An unmeasured composite `inner=X` arm is budgeted
   `X.perf_budget_ms + composite perf_budget_ms` (`battery.arm_budget`); a measured key wins.
9. **Adding a leaf needs only a `policy_golden_v2` entry** (`test_every_dispatch_policy_has_a_golden_entry`
   fails without it, by design). The exact-message tests and the arm list read names from the
   registry (`tests/policy_registry_names.py`). A new arm of a **built-in** must still be
   acknowledged in `_WANT_ARMS`. See the authoring guide §2.
10. **Restart rule.** Edits under `apps/container_logistics/policies/**`,
    `assignment/{app,accounting,pooled_planner,arbitration}.py`, `assignment/policy/*`,
    `assignment/solver/*` need `systemctl --user restart openride-celery`. Selecting a policy
    or params never does. No api rebuild (`run_config.meta` is unschema'd).
11. **Deploy order: backend first.** The dashboard form needs `components` from
    `known-policies` and fails closed without it; the run route's validation answers 503
    against a backend without `validate-run-policies`.
12. **Capture /1 fixtures are refused** (22). Re-capture with `--capture-dispatch`.
    `every=N` counts **dispatch ticks, not sim steps** (first tick, then every N-th).
13. **Notes keys `market` and `pair_notes` are reserved.** A third-party composite setting
    `market` triggers the `meta.market` writer (doc 21 trap 6).
14. **SKIP is not PASS.** The battery reports a declared exemption as `SKIP` (`ok` stays true,
    exit 0, "N passed, k skipped"). Read the skip count, not only the verdict.
15. **The tick's documents are read-only, by contract.** The tick check trusts a sub-scope's
    physical verdict for heads it already proved (keyed on document identity), so a policy
    that mutates and restores a truck or order during a sub-solve can commit infeasible heads.
    A fingerprint check was measured at ~0.9 ms per tick and not adopted; the contract is
    documented in `DispatchPolicy` and pinned by tests. A profile that carries a `policies`
    block needs `policies_format` = the integer 2 (`2.0` and other values are refused as an
    unknown format); a profile with no `policies` block is legacy and accepted.

---

## 7. Verification evidence

**Offline, at `f8b91b9` (coder's gates, 2026-10-22, plus a scratch-copy trial for this doc):**
- Golden: `test_dispatch_golden_identity.py` 70 + `_via_init.py` 15 +
  `test_golden_published_compare.py` 9 = **94 passed**. `dispatch_golden_v1.json.gz` and
  `harness.py` have no commit after `277e67b`.
- Out-of-grid: **204/204 identical** vs `277e67b` (17 compiled bundles × partitioned /
  pooled / env-pooled × 4 ticks).
- Full suite: **37 failed / 2714 passed / 5 skipped / 8 errors**; the failure list equals the
  baseline exactly (the baseline's 2188 passed grew by the new tests only).
- 16/16 shipped specs: compile writes no `policies` into a legacy `spec.json`, the profile is
  `Partitioned{inner: GreedyNearest}` with marker 2, and compile / recompile / compile from
  `$.recipe` give identical agents.
- `openride policy validate dispatch X`, all exit 0: `Partitioned` PASS 47 (3 SKIP),
  `PooledMarket` PASS 187 (8 SKIP), `GreedyNearest` PASS 47, `RandomAssignment` PASS 47
  (2 SKIP). `Partitioned`'s default `no_module_rng` is a SKIP (its own scope-cap draw);
  `[inner=GreedyNearest]` is a real PASS.
- The authoring guide's `RegretMatch` leaf (47, 0 SKIP) and `LargestGroupFirst` composite
  (67, 1 SKIP; passes `sample_rng=ctx.rng`, no `module_rng` declaration) pass the battery on a
  scratch copy of `f8b91b9`. With both registered, `Partitioned` reads 67 (4 SKIP) and
  `PooledMarket` 207 (8 SKIP). The same composite with `sample_rng=random` fails
  `no_module_rng[inner=RandomAssignment]` (exit 1).
- Gate 6 (performance) vs `277e67b`, interleaved, 70 samples per arm: partitioned **1.012
  PASS**, sampled **1.021 PASS**, pooled **1.069** (+19.4 ms/tick vs a +16.1 ms bound):
  **WAIVED** (so not all offline gates are green), attributed to GC-phase noise (1.013 with
  the GC phase held level).
  **Independently re-measured** (n=50/arm): spec protocol pooled 1.059 (+16.6 ms, 0.5 ms over);
  with `gc.collect()`+`gc.disable()` per sample 1.012 (+3.2 ms); steady state both arms run 23
  gen-2 collections per 40 ticks. The GC-phase explanation holds; waiver confirmed. A real
  residual remains: about +2% (~6 ms/tick) in steady state, the operational cost.
- Frontend (analytics `57cba42`): `tsc --noEmit` 0, vitest 601/601.
- `policy list market.offer` lists the three components; `policy list` shows info models.

**Live — PENDING (needs merge + celery restart):**
- [ ] `multi_haulier_greedy_solver_500_trucks` n=3 interleaved: `empty_ratio`,
      `dual_cycle_rate` in band, 100% `osrm`, 0 `DISPATCH POLICY` lines, stamp `label` and
      `information_model` present, run name unchanged.
- [ ] `collab_scale_500t_v2 --structure chain --policy dispatch=PooledMarket` n=3: as above,
      plus `meta.market` present and `benefit == reserve − awarded` (to the 0.001 km rounding)
      on every tag that carries all three values.
- [ ] One `ORSIM_PLANNER_TOPOLOGY=pooled` run whose stamp equals the `--policy` arm.
- [ ] Browser: edit a nested ref param in Operator policies, save, run, confirm the stamp.
- [ ] `--capture-dispatch` run, then `policy bench --fixtures` over it.

---

## 8. NOT done

- **P4 NetworkFlow** (`information_model="central"`, `requires=("ortools",)`; `ortools` is not
  in the venv). Selection, accounting, stamp and battery come free because it is a leaf. It
  still needs `ctx.travel`, facility ids, time windows, executable `timing: "plan"` with held
  tails, and an optional time-budget `fallback` ref.
- **P5**: `idle`, `queue`, `world.shedding`, `world.duration`; `inner_by_company` (needs a
  map-of-refs param type). The cooperation **structure** stays world data.
- The ` · central:<Leaf>` run-name prefix (§3.2 known limit): proposed, not implemented.
- Violations/crashes are not stamped into `run_config`.
- The live gates in §7.

## 9. Files

| Where | Responsibility |
|---|---|
| `policies/framework/` | `slots.py` (5 slots), `params.py` (`PolicyRef`, `ref`, nullable), `registry.py` (`@policy`, discovery, `isolated_registry`), `resolve.py` (merge, CLI parse, `policies_stamp`), `catalog.py`, `components.py`, `errors.py` |
| `policies/dispatch/` | `context.py`, `scopes.py` (only context builders), `runner.py`, `world.py`, `compose.py`, `composite.py`, `legacy.py`, `profile.py`, `resolved.py`, `invariants.py`, `plan.py`, `base.py`, `pair_solver.py`, `capture.py` (/2), `library/{partitioned,pooled_market}.py` |
| `policies/testing/` | whole-tick `fixtures.py`, `battery.py` (`arm_budget`, `Skip`, scoped `module_rng`), `bench.py` |
| `policies/dispatch/helpers/objective.py` | `dual_cycle_cost`, `dual_cycle_cost_fn` (knobs bound once; GreedyNearest's hot loop), `is_dual_cycle` |
| `assignment/accounting.py` | `ShareAccount`, `owner_reserve_km`, `finite_or_none`, `account_shares`, `render_share_tags` |
| `assignment/pooled_planner.py` | market engine only (returns `pair_notes`, no tags) |
| `assignment/policy/{offer,claim}.py`, `assignment/arbitration.py` | components with typed `Params` |
| `assignment/solver/` | the two leaves |
| `assignment/app.py` | `_dispatch_world`, `_dispatch_runner`, `_dispatch_tick`, `_record_round_stats` |
| `scenario/scenario_overrides.py` | `run_dispatch_overrides`, `dispatch_override_label`, `assert_policies_format`, `_profiles_with_current` (source-marker check) |
| `openride_control/policies.py`, `openride/policy_cmd.py`, `openride/scenario_cmd.py` | dry run / validate / bench; CLI |
| `tests/policy_registry_names.py` | registry-derived name lists for the exact-message tests |
| `tests/golden/` | `dispatch_golden_v1.json.gz` (untouched), `compare.py`, `policy_golden_v2.json.gz`, `capture_policy_golden.py --policy` |
| FE | `lib/policy-slots.ts`, `lib/policy-catalog-types.ts`, `components/scenario/{PolicyParamsForm,PolicySlotsEditor}.tsx`, `app/api/scenarios/policies/route.ts`, `lib/runPolicyOverrides.ts`, `app/api/scenario/run/route.ts`, `lib/compareRunMeta.ts`, `lib/runNameMeta.ts` |
