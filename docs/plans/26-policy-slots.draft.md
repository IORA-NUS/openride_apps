# Policy slots — pluggable dispatch, selection, validate/bench/capture (P0-P3) — DRAFT

> **Draft** of `/home/user/docs/reference/features/26-policy-slots.md` (out of repo).
> Written on branch `feat/policy-slots` (worktree `/home/user/openride_apps_policy_slots`),
> **not merged into `/home/user/openride_apps`**, so nothing here is live on this box's
> celery workers or dashboard backend yet. Plan: `docs/plans/policy_slots_p0_p3.md`.
> Authoring guide: `docs/policy_authoring.md`. Needs a CLAUDE.md §5 pointer when it lands.

---

## 1. What landed

Every simulation decision is meant to become a **slot** with registered, typed,
validated **policies**. P0-P3 make the first slot, `dispatch` (which free truck serves
which unassigned order), fully pluggable; the other six slots are declared only.

| Phase | Commit(s) | What |
|---|---|---|
| G0 | `277e67b` | Pre-v2 dispatch golden captured on `ec085f5` (`tests/golden/dispatch_golden_v1.json.gz`, 70 cases), never regenerated since. |
| P0 | `959b056` | `policies/framework`: `SLOTS` (7 slots), typed `PolicyParams`/`param()`, `@policy` registry with ONE discovery function, `policy_catalog()` served as `known-policies.slots`. GreedyNearest/RandomAssignment register in place. **Unknown dispatch names now fail loud** at `get_solver`, `apply_solver_override`, spec compile and run start. The CLI's hand-copied solver/policy lists are gone. |
| P1 | `b143087`, `14dfd81` | Contract v2: `DispatchPolicy.decide(ctx) -> Plan`. Both topologies (partitioned, pooled) build a `DispatchContext` per scope; `DispatchRunner` checks invariants, discards+logs a violating plan, commits chain heads. `PairSolver` keeps the v1 `solve` shape; `BaseAssignmentSolver` is an alias. RandomAssignment gains an opt-in `candidates=spatial` arm. Golden **70/70 byte-identical**, module-RNG consumption included. |
| P2 | `b98d452`, `ba79a87` | `spec.json` top-level `policies` block (`slot -> {type, timing?, params?}`); legacy `solver`/`solverParams`/`planner.deployment` translate into it at compile; disagreeing spellings are refused. Per-run `ORSIM_POLICY_OVERRIDES` (+ `ORSIM_SOLVER` sugar), validated before spawn by `run-simulation`; any `PolicyError` exits 2 before agents spawn. Effective policies + candidate settings stamped in `run_config.meta.policies`. Dashboard **Operator policies** card (`PolicySlotsEditor.tsx`, analytics `681a734`), CLI `--policy`/`--policy-param`. |
| P3 A | `21bd28e` | `policies/testing`: synthetic fixtures, the contract battery (structure, robustness, determinism, performance, params), the single-tick bench; `dispatch/capture.py` (byte-stable `openride.dispatch_context/1` fixtures that replay to the same heads); per-policy golden forcing a `version=` bump. |
| P3 B | (uncommitted at time of writing) | Control `policy-validate` / `policy-bench`; CLI `openride policy list/show/validate/bench` (`solver list` is an alias of `policy list dispatch`); `--capture-dispatch every=N,max=M` on `openride run` and `run-simulation` → `ORSIM_CAPTURE_DISPATCH` → `profile.policy_capture` on every assignment behavior, dir `<datahub>/fixtures/dispatch/<run_id>`; `docs/policy_authoring.md`. |

## 2. Files / modules map

| Where | Responsibility |
|---|---|
| `apps/container_logistics/policies/framework/` | `slots.py` (slot table), `params.py`, `registry.py` (`@policy`, `discover_builtin_policies`, `isolated_registry`), `resolve.py` (spec/profile/override → `ResolvedPolicy`, legacy translation, `parse_policy_cli_args`, `policies_stamp`), `catalog.py`, `errors.py` (`PolicyError(ValueError)` family). |
| `.../policies/dispatch/` | `base.py` (`DispatchPolicy`), `pair_solver.py`, `plan.py`, `context.py`, `scopes.py` (the only context builders), `allowed.py` (`PartitionRule`/`CompanyRule`), `helpers/{candidates,objective}.py`, `views.py`, `invariants.py`, `profile.py`, `runner.py`, `capture.py` (incl. `parse_capture_spec`), `library/` (empty; new policies go here). |
| `.../policies/testing/` | `fixtures.py`, `battery.py`, `bench.py`. |
| `.../assignment/solver/` | The two built-ins (`@policy`, nested `Params`, explicit `decide`); legacy `SOLVER_REGISTRY` view. |
| `.../assignment/app.py`, `pooled_planner.py` | Decide through the runner; `app.py` imports `policies` only inside its accessor. |
| `datagen/preprocess.py`, `scenario/{spec_compile,frontend_scenario_spec,scenario_overrides,scenario_manager}.py` | Spec block, compile-time resolve, overrides, meta mirror. |
| `apps/simulation/run_container_logistics_simulation.py` | Env parsing, overrides, capture mapping, run-start resolve (exit 2), stamp. |
| `openride_control/policies.py`, `command.py`, `manager.py`, `scenario.py` | Run-flag validation, `policy-validate`, `policy-bench`, `known-policies.slots`, env plumbing. |
| `openride/policy_cmd.py`, `control.py`, `run_group.py`, `run_cmd.py`, `runner.py`, `services_cmd.py` | CLI. |
| FE `lib/policy-slots.ts`, `components/scenario/{PolicySlotsEditor,PolicyParamsForm}.tsx`, `lib/scenario-world.ts` | Dashboard editor; `lib/scenario-solvers.ts` deleted. |

## 3. Comparability (plan §5)

- **Every slot at its default is byte-identical** to before (golden: assignments,
  order, share tags, wire payloads, round stats, module-RNG consumption). Runs before
  and after the change are comparable.
- **`RandomAssignment` with `candidates=spatial` is a new arm**, not comparable with
  Random/sampled runs; that difference is the experiment. Read the arm from
  `run_config.meta.policies`.
- The stamp fields (`policies`, `candidate_settings`) are provenance only.
- Changing the spatial knobs or `max_trucks_per_haulier` was already outcome-changing
  (audit P3); it is now visible in the stamp.
- No world slot is implemented, so no world boundary is crossed.
- `policy bench` deadhead is **haversine at decision time**, not the road-distance KPI
  (§6.3); single-tick scores screen, runs (n>=3 interleaved) certify.

## 4. Traps

1. **Module-RNG consumption is part of the contract.** On the partitioned path the
   policy RNG *is* the `random` module, shared with candidate sampling and with trucks'
   `random() <= response_rate`. One extra or missing draw shifts every later draw in
   the worker. `ctx.sampled_trucks()` draws at most once and only when asked; the pooled
   early returns stay ahead of any `decide`. The golden's `rng_probe` locks this.
   Policies must draw from `ctx.rng`, never `random.*` directly.
2. **Import-cycle rule.** `policies/__init__.py`, `policies/framework/*`,
   `policies/dispatch/{base,plan,pair_solver}.py` are leaves and must never import
   `apps.container_logistics.assignment.*` at module top; cycles are order-dependent
   and only show when some module is imported FIRST (`tests/test_policy_import_cycles.py`
   runs each in a subprocess).
3. **Fail-loud flips.** An unknown dispatch name used to fall back to the default with
   a warning. Now `get_solver`, **`apply_solver_override` (per-run `--solver` /
   `ORSIM_SOLVER`) — doc 11 says "Unknown strategy is ignored (warned)"; that is no
   longer true and doc 11 must be updated** — spec compile, and run start all raise
   (run start: exit 2 before agents spawn). The offer/claim/arbitration registries stay
   fail-soft (doc 21 §7).
4. **The live dashboard blocks scenario saves** until the backend it shells out to
   exposes `known-policies.slots`. Analytics `681a734` is committed in the dashboard's
   own checkout, which runs against `/home/user/openride_apps` — the unmerged main tree
   has no `slots`, so the Operator policies card shows "backend does not expose the
   policy catalog" and saving is refused. Merge the backend (user decision, plan §5 F20)
   or expect that.
5. **`NON_WORLD_KEYS` must be split in P5.** FE `lib/scenario-world.ts` strips
   `policies` from the scenario-world identity check; that is correct only while every
   slot is an operator slot. A P5 world slot (`world.shedding`, `world.duration`) must
   NOT be stripped, or two different worlds compare "same".
6. **A violating plan is discarded for that scope and tick** and logged
   `DISPATCH POLICY CONTRACT VIOLATION` at ERROR in **celery's journal**, not `sim.log`.
   Only corrupt input (duplicate ids) can trigger it with the built-ins. Under Random
   this used to double-assign silently.
7. **Restart rule.** Adding/editing a policy file (or any `policies/**` code) needs a
   celery restart; choosing a policy or params never does (data at spawn). The
   `/restart-for` skill table should gain `apps/container_logistics/policies/**`
   (outside the repo — ask the user).
8. **Accounting vs objective (plan §7 risk 8).** `benefit_km` and pooled arbitration
   use the framework's `assignment_cost` with `solver_params` (the one-dial mirror of
   the dual-cycle params). A policy optimising a different objective is still scored
   by that. Audit P5 remains open.

## 5. Verification evidence

**Offline (worktree), run on 2026-10-21:**
- Golden `tests/test_dispatch_golden_identity.py`: **70 passed**.
- `pytest tests/test_cli_*.py tests/test_policy_*.py tests/test_dispatch_*.py tests/test_openride*.py`: **396 passed, 1 failed** (pre-existing: `test_cli_rules_baseline.py::test_rules_baseline_does_not_recompile`; 362 passed before P3 B, +34 in `tests/test_cli_policy_group.py`).
- `openride policy validate dispatch GreedyNearest` / `RandomAssignment`: PASS, 21/21 checks each.
- `openride policy bench dispatch --policies GreedyNearest,RandomAssignment --synthetic scale`:
  1000x500 tick, both assign 500/500; haversine deadhead 41.4 km vs 7,579.6 km; dual
  cycles 204 vs 7; 0 violations.
- Capture path exercised offline: `ORSIM_CAPTURE_DISPATCH` → the sim-main helper →
  `build_runner_for_app` → two fixtures → `policy bench --fixtures`. No live run.

**Live gates — PENDING USER APPROVAL, NOT RUN** (plan §6 item 9; each needs this branch
merged into `/home/user/openride_apps` and a celery restart):
- [ ] `systemctl --user restart openride-celery`.
- [ ] `/gate-run multi_haulier_greedy_solver_500_trucks` n=3 interleaved, and
      `collab_scale_500t_v2 --structure chain` pooled n=3: `empty_ratio` and
      `dual_cycle_rate` in band, 100% `osrm` geometry, 0 contract violations in
      journalctl, `run_config.meta.policies` present, run name unchanged on the default path.
      <!-- evidence -->
- [ ] Browser (Playwright): edit `dual_cycle_bonus_km` in Operator policies, save,
      confirm `spec.json` + bundle profile, run, confirm `run_config.meta.policies.dispatch.params`.
      <!-- evidence -->
- [ ] One `openride run <slug> --capture-dispatch every=10,max=40` run, then
      `openride policy bench dispatch --policies GreedyNearest,RandomAssignment --fixtures datahub/fixtures/dispatch/<run_id>`.
      <!-- evidence -->

## 6. Not done

- **P4**: NetworkFlow (`library/network_flow.py`, `requires=("ortools",)`), `ctx.travel`
  / `ctx.facilities` / `ctx.state`, `timing: "plan"` executable with `held` tails, time
  budgets with a declared fallback, and `meta.policy_runtime` provenance.
- **P5**: `queue`, `idle`, `world.shedding`, `world.duration` slots; `market.offer`/
  `market.claim` moved under `policies.*`; dead keys retired; per-haulier policy config;
  the `NON_WORLD_KEYS` split.
- **Violations are not stamped** into `run_config` (only the journal ERROR line and the
  runner's in-memory stats); P4's `meta.policy_runtime` is where they land.
- The live gates in §5.
- Doc 11 update (fail-loud override) and the CLAUDE.md §5 pointer.

## 7. Files

See §2. Tests: `tests/test_policy_*.py`, `tests/test_dispatch_*.py`,
`tests/test_cli_policy_group.py`, `tests/test_cli_no_hardcoded_policy_lists.py`,
`tests/test_policies_spec_{block,carry}.py`, `tests/test_random_spatial_candidates.py`,
`tests/test_frontend_editform_policy_slots.py`, goldens under `tests/golden/`.
