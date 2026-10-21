# Policy Slots, P0 to P3: implementation plan for the coder

All paths below are in the worktree `/home/user/openride_apps_policy_slots` (branch `feat/policy-slots`, HEAD `ec085f5`) unless they are marked `FE:`. `FE:` means `/home/user/openride_server/analytics` (branch `feat/policy-slots`). I made no changes to either tree. Every fact in §1 was checked against the code.

---

## 1. Current-state facts the plan relies on (verified), and where the design text is wrong

### 1.1 Facts

| # | Fact | Where |
|---|---|---|
| F1 | `SOLVER_REGISTRY` is a hardcoded dict holding exactly the two solvers. `DEFAULT_SOLVER = "RandomAssignment"`. `get_solver()` silently falls back to the default and only logs a warning. | `assignment/solver/__init__.py:15-18, 20, 23-42` |
| F2 | `BaseAssignmentSolver` has `solve(trucks, orders, *, pair_allowed, cost)` (abstract) plus the opt-in injections `set_rng`/`rng`, `set_tiebreak`/`tiebreak_seed` and `set_rebate_book`/`rebate_book`. When nothing was injected, `rng` is the **`random` module**. | `assignment/solver/base.py:17-119` |
| F3 | Only `GreedyNearestSolver` has `solve_pairs`. With no tiebreak seed it does a module-RNG shuffle followed by a stable sort; with a seed it sorts by a blake2b tiebreak. Random's `solve` shuffles the orders, then for each order shuffles its candidates. | `solver/greedy_nearest.py`, `solver/random_assignment.py` |
| F4 | Partitioned path. The spatial path is chosen by `use_spatial and hasattr(self._solver, "solve_pairs")` at **app.py:656**, not 667 (667 is the orders-by-haulier grouping). Its fallback samples with the **module** RNG (`random.sample(bt, max_trucks_per_haulier)`, app.py:693-697). That fallback applies to **Greedy too** when `use_spatial_matching=false`. | `assignment/app.py:603-698` |
| F5 | Pooled path. The same hasattr check appears **twice**, at pooled_planner.py:327 and :466. Its fallback samples with `ctx.rng.sample` (the tick-seeded `Random`). Before solving it returns early: `[]` when there are no prebuilt pairs (spatial path), or when there are no trucks or no orders (sampled path). `set_rng`/`set_tiebreak` are injected only on this path (app.py:457-471). | `assignment/pooled_planner.py:311-341, 444-478` |
| F6 | Objective: `assignment_cost` (bonus 5.0, radius 0.5, clamped at 0). A single dict, `profile.solver_params`, drives **both** the solver's ranking and the framework's accounting (`benefit_km`, owner reserve, claim bids, arbitration). | `constraints.py:136-188`; app.py `_cost` at :444 and :618 |
| F7 | Spatial defaults are 0.02 / 40 / 8, read from assignment-profile keys that appear in no `spec.json`, so they are not stamped anywhere (audit P3). | `spatial.py:416-418`, app.py:550-553 |
| F8 | Datagen `@register(role,name)` plus `known_policies()` already exists. The control command **`known-policies` already exists** and returns datagen roles, `{policies:{truck,order,facility}, defaults}`. It is consumed by `FE:app/api/scenarios/policies/route.ts` and `openride/scenario_cmd.py:583`. | `datagen/agents/registry.py:78-103`; `frontend_scenario_spec.py:2091-2105`; `openride_control/command.py:157-158` |
| F9 | Hand-copied lists: `openride/config.py:78-79` has `SOLVERS` and `DEFAULT_SOLVER = "GreedyNearest"`, which **disagrees** with the registry default `RandomAssignment`. `config.py:84-89` has `POLICIES` (datagen). They are used by argparse `choices=` at `scenario_cmd.py:961, 970-975` and `run_group.py:49, 178`. | |
| F10 | More hardcoded lists the design missed: **`FE:lib/runNameMeta.ts:23 KNOWN_SOLVERS`**, used by `FE:app/api/scenario/run/route.ts:54` to reject the run; `FE:lib/scenario-solvers.ts`, used only by `ScenarioTab.tsx:46,102,739-758`; `frontend_scenario_spec.py:26 KNOWN_SOLVERS` (frozen at import) together with `normalize_solver` (silent coercion, line 29), which is used by the editForm at :1532 and by legacy paths. | |
| F11 | **A third legacy dial exists: `planner.deployment.{type,params}`.** Precedence today is `solver` > `planner.deployment.type` > default, and the winner is mirrored into `deployment`. `solver_params` = defaults ⊕ `deployment.params` ⊕ `solverParams`. The recipe echoes `solver` and `solverParams`. | `datagen/preprocess.py:566-590, 662-664` |
| F12 | `planner.timing.mode ∈ {online, static, hybrid}` is validated at compile time but nothing reads it. All 16 shipped specs use `online`. | `preprocess.py:54, 173-187` |
| F13 | `SPEC_KEYS` (carry policy) and `SPEC_KEY_NULL_POLICY` are mandatory and checked at import. An unregistered top-level key is **silently dropped on save**. `tests/test_assemble_spec_key_registry.py` forces a sentinel for every key and checks key order. | `frontend_scenario_spec.py:447-612` |
| F14 | `edit_scenario` merges shallowly: `{**base, **patch}`. | `frontend_scenario_spec.py:2048` |
| F15 | Per-run override: `ORSIM_SOLVER` → `apply_solver_override` patches only `profile.strategy` and **ignores an unknown name with a warning**. The run name gets `· <solver>` appended only when an override is applied. | `scenario_overrides.py:10-41`; `run_container_logistics_simulation.py:41-46, 196-206` |
| F16 | `run_config.meta` = `ScenarioManager.get_run_config_meta()`. It mirrors `orsim_settings` keys (`rebate`, `facility_rules`) without adding a new writer, so the policies stamp can be added the same way. | `scenario_manager.py:90-120` |
| F17 | No pydantic and no jsonschema in requirements or venv (Python 3.11.2). **Use dataclasses.** | `requirements.txt` |
| F18 | Tests build `AssignmentApp` with `__new__` (no `__init__`) and set `_solver`, `_solver_params`, `behavior`, `manager` by hand. One sets `_solver = RandomAssignmentSolver()` with **no `strategy` in the profile** (`test_container_logistics_hauliers.py:124-132`). Tests also monkeypatch `spatial.iter_candidate_pairs`, `pooled_planner.build_round_candidates` and `BaseAssignmentSolver.set_tiebreak` as **module/class attributes**, and they read `app._solver.rebate_book`. | tests listed in §7 |
| F19 | `assignment/__init__.py` eagerly imports `agent` → `app`, and the agent class path `apps.container_logistics.assignment.AssignmentAgent` depends on it (`container_logistics_wiring.py:129`). Any `policies` module that imports `assignment.*` while `app.py` imports `policies` at module top **will cycle**. | |
| F20 | Celery runs from the **main tree** (`WorkingDirectory=/home/user/openride_apps`), and so does the dashboard's python shell-out (`FE:lib/workspacePaths.ts`). The main tree has **uncommitted edits to `assignment/app.py` and `run_container_logistics_simulation.py`** (Kafka transport). | `~/.config/systemd/user/openride-celery.service`; `git status` in the main tree |
| F21 | The offer, claim and arbitration registries are **fail-soft by documented design** ("A typo … must degrade a run, never abort it"). | doc 21 §7; `policy/offer.py`, `policy/claim.py`, `arbitration.py` |
| F22 | The scenario-world identity check strips `NON_WORLD_KEYS` (`planner`, `solver`, `solverParams`, …). | `FE:lib/scenario-world.ts:17-27` |
| F23 | The worktree has no compiled `scenario.json`. Compiled bundles exist only in `/home/user/openride_apps/scenarios/*/scenario.json`. | |

### 1.2 Where the design text is wrong or needs adjusting

1. **"`known-policies` is extended."** It already exists for datagen. Extend its payload with a new top-level key `slots` and leave `policies`/`defaults` untouched. The word "policies" already means datagen role policies in the editForm (`editForm.policies`) and in `ScenarioTab` state. The new editForm/frontend field is therefore named **`policySlots`**. The spec.json key stays `policies`, as approved.
2. **There are more hand-copied lists than the design names**, and there is a third legacy dial. See F9, F10 and F11.
3. **`ctx.rng` cannot be "seeded from (run_id, step, slot), module-global off-limits" in this round.** The partitioned path consumes the **module** RNG today (F4/F5), and the byte-identical gate forbids changing that. This round, `ctx.rng` mirrors whatever today's path uses. Seeding partitioned runs is a separate comparability boundary for later.
4. **"Shuffling input order gives the same decision, required of every policy" cannot be required of RandomAssignment** (shuffle-based), or of partitioned Greedy without a tiebreak seed. The battery requires reproducibility from every policy. It requires input-order independence only when a tiebreak seed is set and the policy is not registered with `order_sensitive=True` plus a stated reason (Random gets one).
5. **Code layout.** Keep GreedyNearest and Random **where they are (`assignment/solver/`)**, decorated in place. Moving them creates the F19 import cycle and widens the diff under the hard gate. Old import paths stay native, so no re-export shims are needed for them. New dispatch policies go in `policies/dispatch/library/`.
6. **"Objective moved out of constraints.py."** The arithmetic stays in `constraints.assignment_cost`, so byte-identity holds by construction. `helpers/objective.py` exposes it as policy helpers. Physically moving it is cosmetic and deferred.
7. **"Violations … stamped on the run", and notes aggregated into round_stats.** Stamping them would add an agent-side `run_config` writer on the default partitioned path, which is the doc 21 trap 6 class. This round: log ERROR, count in memory, discard the plan, and fail the battery. Stamping is deferred to P4, together with notes and the timeout fallback.
8. **Dead `planner.timing`.** `online` is accepted silently. `static`/`hybrid` become a compile error that names the translation. No shipped spec uses them.
9. **`field(...)` is named `param(...)`**, to avoid shadowing `dataclasses.field`.
10. **The fail-loud principle conflicts with doc 21 §7.** Only the dispatch slot flips to fail-loud this round. `market.offer` and `market.claim` are listed read-only in the catalog with status `legacy`, are still configured through `planner.market.*`, and stay fail-soft. I am stating this deliberately rather than leaving it implicit.
11. **The live gates need the main tree.** That covers the 500-truck n=3 run and the browser check against the real dashboard (F20), so neither can pass from the worktree alone. See §5 and §6.

---

## 2. House-style rules for the coder

1. **Module docstring on every new module.** It states the module's single responsibility, its import or purity contract, and the boundary (framework vs policy). Precedents: `rebate.py`, `pooled_planner.py`. Point to doc sections (`docs/policy_authoring.md §N`, doc 21 §5.2) instead of retelling history. Do not add new multi-paragraph provenance essays.
2. **Function docstrings state the contract** (inputs, outputs, raises). Inline comments only explain *why* for non-obvious lines, for example "module RNG on purpose, byte-identity, see §4.4". Do not narrate what the code does.
3. Use `from __future__ import annotations`. In new code use builtin generics (`dict[str, Any]`). Keep `typing.Dict` where an existing file already uses it. Use `@dataclass(frozen=True)` for value objects and plain functions over classes when there is no state. Every module gets `__all__`.
4. Logging: `logger = logging.getLogger(__name__)`. Loud conditions use `logger.error` with an ALL-CAPS greppable prefix (the precedent is "POOLED MARKET TRUNCATED BY BACKSTOP"). Agent-side logs go to celery's journal, not `sim.log`, so say so in messages that matter.
5. Errors: every config error subclasses `PolicyError(ValueError)`, so existing `except ValueError` paths still map to `INVALID`. At compile time, wrap into `SpecValidationError(str(exc))` so that remains the single validation surface. Messages name **the offending key and value, the allowed set, and the fix**, as in §4.
6. **No silent fallbacks** for a dispatch name, params, timing or dependencies. Boolean flags get no truthiness coercion (precedent: `preprocess._strict_bool`). `bool` is not accepted where a number is expected. Non-finite numbers never cross a boundary (doc 21 FIX-5).
7. **Import-cycle rule.** These are leaf modules and must never import `apps.container_logistics.assignment.*`: `policies/__init__.py`, `policies/framework/*`, `policies/dispatch/{base,plan,pair_solver}.py`. `assignment/app.py` imports from `policies.dispatch` **only inside the one accessor method**, with a local import commented with this rule. The precedent is the local import in `pooled_planner.effective_cooperation_stamp`. `pooled_planner.py` does not import `policies` at all: it receives the runner object through its context.
8. **Monkeypatch-compatible call sites.** Call `spatial.iter_candidate_pairs(...)` and `build_round_candidates(...)` through their module (`from apps.container_logistics.assignment import spatial` then `spatial.iter_candidate_pairs`). Never bind them with `from … import name`. `set_tiebreak` stays a method inherited from the class that tests patch.
9. **`AssignmentApp` harness rule.** New code in `app.py` may rely only on `behavior`, `manager`, `_solver`, `_solver_params`, `_warned_haulier_block` and `_share_tags`. Everything else is read with `getattr(self, "x", default)` (for example `run_id` and `_sim_horizon_steps`), because tests build the app with `__new__`.
10. Test style, matching the repo: plain pytest functions named `test_<behaviour in words>`, and a module docstring saying what is locked and why. Use small `_truck`/`_order` factories (reuse `tests/test_assignment_cooperation.py` helpers) and assertion messages that explain the failure. For every invariant test, mutation-check it (break the code, see the test fail) and write "Verified to have teeth: …" in its docstring.
11. Frontend: keep pure logic in `lib/<name>.ts` with a colocated `lib/<name>.test.ts` (vitest). Components start with `'use client'` and carry a JSDoc block explaining why. Reuse the existing Tailwind vocabulary from `ScenarioTab`: card `rounded-xl border border-white/10 bg-white/[0.02] p-4`, select `rounded-lg border border-white/15 bg-black/40 px-3 py-2 font-mono text-xs text-white/90`, label `font-mono text-[11px] uppercase tracking-wide text-white/45`. Use `NumberField` for numbers and dropdowns, not searchable lists. Do not change the palette.
12. One responsibility per module, short functions, no metaclasses, no `__init_subclass__` tricks. Auto-discovery is allowed in exactly one documented function (§3).

---

## 3. Final module layout

**New: `apps/container_logistics/policies/`**

| File | Single responsibility |
|---|---|
| `__init__.py` | Docstring only (package map plus the import-cycle rule). **No imports.** |
| `framework/__init__.py` | Re-exports the public API: `policy`, `PolicyParams`, `param`, the errors, `get_policy_spec`, `list_policies`, `SLOTS`. |
| `framework/errors.py` | `PolicyError(ValueError)`, plus `UnknownSlotError`, `SlotNotImplementedError`, `UnknownPolicyError`, `PolicyParamsError`, `PolicyTimingError`, `PolicyDependencyError`, `PolicyRegistrationError`, `DispatchContractViolation`. |
| `framework/slots.py` | `SlotSpec` and the `SLOTS` table (7 slots). |
| `framework/params.py` | `PolicyParams` base, `param()`, validation (`from_mapping`), `to_dict`, `json_schema`, `SUPPORTED_SCHEMA_KEYWORDS`. |
| `framework/registry.py` | `PolicySpec`, the `@policy` decorator, lookup, `discover_builtin_policies()`, `isolated_registry()` (for tests). |
| `framework/resolve.py` | Spec/profile/override → `ResolvedPolicy`. Also legacy translation, override merging and the run stamp. |
| `framework/catalog.py` | `policy_catalog() -> dict`, the JSON returned by `known-policies.slots`. |
| `dispatch/__init__.py` | Docstring only. |
| `dispatch/base.py` | `DispatchPolicy` ABC (leaf). It holds the injection API moved verbatim from `BaseAssignmentSolver`, plus abstract `decide(ctx) -> Plan`. |
| `dispatch/plan.py` | `Plan` (leaf). |
| `dispatch/pair_solver.py` | `PairSolver(DispatchPolicy)`: the v1 `solve` contract plus a default `decide()` adapter (leaf). |
| `dispatch/allowed.py` | `PartitionRule` and `CompanyRule`. These are the feasibility predicates as data, moved verbatim from app.py `_pair_allowed` and from pooled `_pair_allowed_for`. |
| `dispatch/helpers/__init__.py` | Empty. |
| `dispatch/helpers/candidates.py` | `CandidateSettings`, `spatial_pairs_for_partition`, `sampled_trucks`, `full_pairs`. |
| `dispatch/helpers/objective.py` | `deadhead_km`, `is_dual_cycle`, `dual_cycle_cost` (delegates to `constraints`). |
| `dispatch/views.py` | `TruckView` and `OrderView`: typed, read-only, each carrying `.raw`. |
| `dispatch/context.py` | `DispatchContext`, `DispatchClock`, `Scope`. |
| `dispatch/scopes.py` | `partition_context(...)` and `company_context(...)`, the only places a context is built. |
| `dispatch/invariants.py` | `check_plan(plan, ctx) -> list[str]`. |
| `dispatch/profile.py` | Assignment profile → resolved dispatch (`policies` block or legacy keys), and `build_dispatch_policy`. |
| `dispatch/runner.py` | `DispatchRunner`: decide, check, take heads, map ids back to docs, record stats, call the capture hook. |
| `dispatch/capture.py` | (P3) `CaptureSettings`, `context_to_fixture`, `context_from_fixture`, `maybe_capture`. |
| `dispatch/library/__init__.py` | Empty package that discovery walks; P4 puts `network_flow.py` here. |
| `testing/__init__.py` | Empty. |
| `testing/fixtures.py` | (P3) Synthetic contexts: empty, single truck, no geometry, one haulier, infeasible, scale. |
| `testing/battery.py` | (P3) `CheckResult`, `DISPATCH_CHECKS`, `run_battery(slot, name)`. |
| `testing/bench.py` | (P3) `load_fixtures`, `bench(names, fixtures, overrides) -> BenchReport`, `render_table`. |

**Changed (backend)**

- `assignment/solver/__init__.py`: static legacy `SOLVER_REGISTRY`, `DEFAULT_SOLVER` taken from the slot, fail-loud `get_solver`.
- `assignment/solver/base.py`: `BaseAssignmentSolver = PairSolver` (alias) plus re-exported type aliases.
- `assignment/solver/greedy_nearest.py` and `random_assignment.py`: `@policy`, nested `Params`, explicit `decide`. Random gains `solve_pairs`.
- `assignment/app.py`: `__init__` resolution, `_dispatch_runner(prof)`, `_solve_bucket` going through the runner, extra fields on the pooled context, and (P3) capture.
- `assignment/pooled_planner.py`: `PooledPlannerContext.dispatch` replaces `.solver`; `_solve_company` goes through the runner; rules come from `CompanyRule`.
- `datagen/preprocess.py`: P2 `_normalize_policies`, recipe and mirrors.
- `scenario/spec_compile.py`: P0 name check; P2 resolve and patch the assignment behaviors.
- `scenario/frontend_scenario_spec.py`: `SPEC_KEYS["policies"]`, the supersede helper, editForm `policySlots`, registry-derived `known_solvers` and `known_dispatch_names()`.
- `scenario/scenario_overrides.py`: `apply_policy_overrides` (new); `apply_solver_override` becomes a fail-loud wrapper.
- `scenario/scenario_manager.py`: meta mirror `"policies"`.
- `apps/simulation/run_container_logistics_simulation.py`: env parsing, overrides, run-start resolve and stamp, run-name label, (P3) capture settings.
- `openride_control/{command.py, manager.py, scenario.py}`, plus new `openride_control/policies.py` (validate and bench commands).
- `openride/{config.py, control.py, scenario_cmd.py, run_group.py, run_cmd.py, runner.py, services_cmd.py, cli.py}`, plus new `openride/policy_cmd.py`.
- New `docs/policy_authoring.md`.

**Changed (FE)**

- `app/api/scenarios/policies/route.ts`: also returns `slots`.
- `app/api/scenario/run/route.ts`: no local solver list; forwards `policies`.
- New `lib/policy-slots.ts` and `lib/policy-slots.test.ts`.
- New `components/scenario/PolicySlotsEditor.tsx` and `components/scenario/PolicyParamsForm.tsx`.
- `components/scenario/ScenarioTab.tsx`.
- `lib/scenario-form-snapshot.ts`, `lib/order-demand-curve.ts` (editForm type).
- `lib/scenario-world.ts` and its test.
- `lib/runNameMeta.ts` and its test.
- **Delete** `lib/scenario-solvers.ts`.

---

## 4. Work items per phase

### Step G0: capture the golden fixture BEFORE touching any production code (commit #1, alone)

Files:
- `tests/golden/__init__.py` (empty).
- `tests/golden/capture_dispatch_golden.py`, a script. It may import only pre-change public APIs: `AssignmentApp`, `get_solver`, `compile_spec_to_bundle`, `datagen.hauliers`, plus the stub manager and messenger copied from `tests/test_assignment_cooperation.py`.
- `tests/golden/dispatch_golden_v1.json.gz`.
- `tests/test_dispatch_golden_identity.py`.

Procedure:
1. **Worlds.** Compile `scenarios/collab_scale_500t_v2/spec.json` (500 trucks, 4 hauliers, structures `no-coop`/`chain`) and `scenarios/multi_haulier_greedy_solver_500_trucks/spec.json` (500 trucks, 3 hauliers) with `compile_spec_to_bundle` into a `tempfile.mkdtemp()` dir, with `ORSIM_SCENARIOS_DIR` set to a tmp root. Also add the tiny `_tied_fleet()` world from `tests/test_assignment_pooled_determinism.py` (all exact cost ties).
2. **Tick states.** Use a separate `random.Random(state_seed)`, never the module RNG. Each tick state yields:
   - **Truck docs:** `{_id: agent_id, state: "online"` (10% `"offline"`)`, haulier_id, profile: {haulier_id, truck_size, restricted_areas, home_facility_name, current_loc: init_loc` or a random facility point`}}`. 30% of trucks get `last_dropoff_loc` and `last_dropoff_facility_name` set to a pickup facility of some order, which creates dual-cycle zero-cost ties.
   - **Order docs:** a window of orders by `request_time_step`, as `{_id, state:"unassigned", haulier_id, pickup_loc, dropoff_loc, order_size, profile:{haulier_id, pickup_facility_name, dropoff_facility_name, pickup_loc, dropoff_loc, order_size}}`.
   - **Busy and claimed ids:** `busy_ids` is 10% of trucks; `claimed_order_ids` is 2% of orders.
   - **The tick states themselves:** A = collab dense (500 orders), B = collab sparse (120 orders), C = multi_haulier (400 orders), T = tied.
3. **Case grid,** with tick states stored once and cases referencing them:
   - strategy ∈ {GreedyNearest, RandomAssignment}
   - topology ∈ {partitioned, pooled}
   - active structure ∈ {chain, no-coop} (collab only)
   - `use_spatial_matching` ∈ {true, false} (Greedy only)
   - `max_trucks_per_haulier` ∈ {500, 40}; 40 makes the sample cap bind, which it never does at 125 trucks/haulier
   - module seed 11, `time_step` 42, `run_id` "run_golden"
   - profile otherwise = the compiled assignment profile
   Target 60-80 cases. Every axis value must appear at least once in combination with each of the others it interacts with. Write a coverage matrix in the fixture header.
4. **Per case:** build the app exactly like `tests/test_assignment_pooled._app` (`__new__`, `_solver = get_solver(strategy, profile.solver_params)`, `_solver_params = profile.solver_params`). Then call `random.seed(case.module_seed)`, call `assign(sim_clock, time_step)`, then `publish(result)` with the stub messenger, then `rng_probe = random.random()`. Record:
   - `pairs`: ordered `[[truck_id, order_id], …]`, in order
   - `share_tags`: sorted `[[tid, oid], tag]`
   - `published`: list of `(topic, payload_json_string)`, **the exact strings**
   - `round_stats`: `app.round_stats()` for pooled cases
   - `rng_probe`
5. **Teeth asserts inside the script** (fail the capture otherwise):
   - at least 90% of cases have more than 0 pairs
   - some chain cases have share tags
   - some cases sample (cap 40)
   - tick T yields only cost 0.0
6. **Byte-stable file:** `json.dumps(sort_keys=True, separators=(",",":"))` and `gzip.GzipFile(mtime=0)`. Run the script twice, and under `PYTHONHASHSEED=0` and `=1`, then `cmp` the outputs. They must be identical, otherwise fix the capture.
7. **Replay test.** `tests/test_dispatch_golden_identity.py` parametrizes over `case_id` and rebuilds each app **through the same harness**. Assert:
   - `test_golden_case_is_byte_identical[<case_id>]`: pairs (ordered), share_tags, published strings, round_stats and `rng_probe` are all equal.
   - `test_golden_fixture_header_records_capture_commit` (the header holds the capture git sha) and `test_golden_fixture_has_teeth` (re-checks step 5 on the stored data).
   The runtime must stay under 60 s. If it is slower, trim the grid but keep the coverage matrix.
8. Commit #1 message: `Capture pre-v2 dispatch golden outputs (captured on ec085f5)`. The test must pass on that commit. The reviewer can independently re-run the script on `ec085f5` in a scratch worktree and `cmp` the fixture.

The harness constructs the app through `get_solver` and `__new__`. After P1 that is still a valid path (F18), and after P1 it exercises the new runner because `assign()` builds it lazily (§4 P1.6). In addition, P1 adds `test_golden_case_via_init[...]` for a subset: build the app via the real `__init__` with a stub `ORSimApp` init (monkeypatch `ORSimApp.__init__` to set the attributes), so the `__init__` resolution path is covered too.

### P0: framework core, the single catalog, fail-loud lookup

1. **`framework/slots.py`**
   ```python
   @dataclass(frozen=True)
   class SlotSpec:
       name: str; kind: Literal["operator","world"]
       status: Literal["implemented","legacy","planned"]
       default: str | None; description: str
       timings: tuple[str, ...] = ()           # dispatch: ("online","plan")
       executable_timings: tuple[str, ...] = () # dispatch: ("online",)
       lands_in: str | None = None              # "P4"/"P5" for messages
   ```
   The 7 slots:

   | Slot | Kind | Status | Default | Notes |
   |---|---|---|---|---|
   | `dispatch` | operator | implemented | "RandomAssignment" | timings ("online","plan"), executable ("online",) |
   | `market.offer` | operator | legacy | "OfferAll" | configured via `planner.market.offer` |
   | `market.claim` | operator | legacy | "ClaimAllPlanned" | |
   | `idle` | operator | planned | — | P5 |
   | `queue` | operator | planned | — | P5 |
   | `world.shedding` | world | planned | — | P5 |
   | `world.duration` | world | planned | — | P5 |

   `slot_spec(name) -> SlotSpec` raises `UnknownSlotError`.
2. **`framework/params.py`**
   ```python
   def param(default, *, help: str, ge=None, gt=None, le=None, lt=None,
             choices: tuple[str, ...] | None = None) -> Any  # dataclasses.field(default=..., metadata={...})
   class PolicyParams:   # authors subclass with @dataclass(frozen=True)
       @classmethod
       def from_mapping(cls, raw: Mapping[str, Any] | None, *, where: str) -> Self
       def to_dict(self) -> dict[str, Any]
       @classmethod
       def json_schema(cls) -> dict
   SUPPORTED_SCHEMA_KEYWORDS = frozenset({"type","properties","additionalProperties","default",
       "minimum","exclusiveMinimum","maximum","exclusiveMaximum","enum","description","title"})
   ```
   - Supported annotation types are `bool`, `int`, `float` and `str`, resolved with `typing.get_type_hints`. `choices` is allowed only on `str`.
   - Coercion: an `int` is accepted for a `float` field. `bool` is rejected for `int`/`float`. Strings are **never** coerced. Non-finite values are rejected. Unknown keys are rejected.
   - The schema is `{"type":"object","additionalProperties":false,"properties":{k:{type, default, bounds, enum, description=help}}}`.
   - Error messages (`PolicyParamsError`):
     `"{where}: unknown key 'bonus_km'. Declared params: candidates, dual_cycle_bonus_km, dual_cycle_radius_km."`
     `"{where}: 'dual_cycle_bonus_km' must be >= 0 (got -1.0)."`
     `"{where}: 'candidates' must be one of ['sampled', 'spatial'] (got 'near')."`
     `"{where}: 'dual_cycle_bonus_km' must be a number (got 'five', a str). Values are not coerced from strings."`
     `where` reads like `policies.dispatch.params for GreedyNearest`.
3. **`framework/registry.py`**
   ```python
   @dataclass(frozen=True)
   class PolicySpec:
       slot: str; name: str; version: str; description: str
       requires: tuple[str, ...]; cls: type; params_cls: type[PolicyParams]
       order_sensitive: str | None   # reason, or None
       perf_budget_ms: int           # battery only (P3)
       def missing_requirements(self) -> list[str]   # importlib.util.find_spec
       def kind(self) -> str                         # from SLOTS
   def policy(*, slot: str, name: str, version: str, description: str,
              requires: Sequence[str] = (), order_sensitive: str | None = None,
              perf_budget_ms: int = 2000) -> Callable[[type], type]
   def get_policy_spec(slot: str, name: str) -> PolicySpec          # UnknownPolicyError
   def spec_for_class(cls: type) -> PolicySpec | None
   def list_policies(slot: str | None = None) -> list[PolicySpec]   # sorted by (slot, name)
   def default_policy_name(slot: str) -> str
   def discover_builtin_policies() -> None   # idempotent
   @contextmanager
   def isolated_registry() -> Iterator[None]  # snapshot/restore, tests only
   BUILTIN_POLICY_PACKAGES = ("apps.container_logistics.assignment.solver",
                              "apps.container_logistics.policies.dispatch.library")
   ```
   Registration raises `PolicyRegistrationError` for any of these:
   - an unknown slot
   - a slot whose status is not `implemented` (message: `"slot 'idle' is declared but not implemented yet (lands in P5)"`)
   - a missing nested `Params`, or one that is not a dataclass subclass of `PolicyParams`
   - defaults that fail their own validation
   - `(slot, name)` already registered to a *different* class (re-registering the same class is idempotent)
   - from P1 on, for slot `dispatch`, a class that is not a subclass of `DispatchPolicy`

   Discovery imports `pkgutil.iter_modules` names in sorted order, skips names starting with `_`, and lets import errors propagate. Lookups call `discover_builtin_policies()` first.

   `UnknownPolicyError` message:
   `"Unknown dispatch policy 'GreedyNeerest'. Registered dispatch policies: GreedyNearest, RandomAssignment. (List them: openride policy list dispatch)"`
4. **Register the built-ins in place.** In `greedy_nearest.py`:
   ```python
   @policy(slot="dispatch", name="GreedyNearest", version="1",
           description="Globally greedy nearest-pair sweep, dual-cycle aware.")
   class GreedyNearestSolver(BaseAssignmentSolver):
       @dataclass(frozen=True)
       class Params(PolicyParams):
           candidates: str = param("spatial", choices=("spatial", "sampled"), help=...)
           dual_cycle_bonus_km: float = param(5.0, ge=0, help=...)
           dual_cycle_radius_km: float = param(0.5, ge=0, help=...)
   ```
   `RandomAssignmentSolver` gets `Params(candidates="sampled", choices=("sampled","spatial"))` and `order_sensitive="byte-identical to the pre-v2 shuffle; order independence would change existing results"`. Its `perf_budget_ms` is set in P3 after measuring. In P0 there is no `decide` yet: the dispatch base-class check arrives in P1.
5. **`assignment/solver/__init__.py`.** `SOLVER_REGISTRY` stays as a static dict of the two classes, with a docstring "frozen legacy view; read the policy registry". Then:
   - `DEFAULT_SOLVER = SLOTS["dispatch"].default`
   - `get_solver(name, params=None)`: `spec = get_policy_spec("dispatch", name or DEFAULT_SOLVER)`, then `return spec.cls(params=dict(params or {}))`. **No fallback.**
6. **Fail loud at compile time.** In `spec_compile.compile_spec_to_bundle`, after `Preprocessor.compile`, call `get_policy_spec("dispatch", compiled.spec.assignment_settings["profile"]["strategy"])`. On `UnknownPolicyError`, raise `SpecValidationError(str(e))`.
7. **Fail loud at run start.** New `apps/container_logistics/policies/dispatch/profile.py::assert_assignment_profiles_resolvable(collection) -> None` raises `PolicyError`. In `run_container_logistics_simulation.main`, call it right after the overrides block and before the cooperation stamp. On `PolicyError`: `print(f"Policy configuration invalid; refusing to start the run: {e}")` then `sys.exit(2)`.

   `apply_solver_override` with an unknown name now **raises** `UnknownPolicyError`. This is an intended contract change; update its docstring and doc 11. `AssignmentApp.__init__` lets `get_solver` raise. Agent-side, wrap the error with the suffix: `" If you just added this policy, restart the celery workers (systemctl --user restart openride-celery): agents import policy code once at worker start."`
8. **Catalog.** `framework/catalog.py::policy_catalog() -> dict`:
   ```json
   {"dispatch": {"kind":"operator","status":"implemented","default":"RandomAssignment",
     "timings":["online","plan"],"executable_timings":["online"],"description":"…",
     "policies":[{"name","version","description","requires":[],"missing":[],
                  "available":true,"order_sensitive":null,
                  "params_schema":{…},"defaults":{…}}]},
    "market.offer": {"kind":"operator","status":"legacy","default":"OfferAll",
                     "names":["OfferAll","OfferNone","OfferSpare"],
                     "configured_via":"planner.market.offer"},
    "idle": {"kind":"operator","status":"planned","lands_in":"P5"}, …}
   ```
   - The legacy names are read from `OFFER_REGISTRY`/`CLAIM_REGISTRY` at call time.
   - `openride_control/scenario.py::cmd_known_policies` returns `{**known_policies(), "slots": policy_catalog()}`.
   - `frontend_scenario_spec.known_solvers()` returns `{"solvers": [p.name for p in list_policies("dispatch")], "default": default_policy_name("dispatch")}`.
   - Delete `KNOWN_SOLVERS` (line 26). `normalize_solver` reads names at call time; it stays legacy-only and is documented as such.
9. **Delete the CLI lists.**
   - `openride/config.py`: remove `SOLVERS`, `DEFAULT_SOLVER`, `POLICIES` and `DEFAULT_POLICIES`.
   - Argparse: drop `choices=` and say in the help "validated by the engine; list: openride policy list dispatch". For the datagen help, point at `openride scenario` TUI or `known-policies`.
   - `run_group._pick_solver` uses `control.known_solvers()`.
   - `scenario_cmd._known_solvers` and `_known_policies`: delete the config fallbacks; a `ControlError` propagates to a red message.
   - `command.py:85` help: "a registered dispatch policy name".

**P0 tests**
- `tests/test_policy_framework_registry.py`:
  - `test_policy_decorator_registers_and_returns_the_class`
  - `test_same_class_reregistration_is_idempotent`
  - `test_duplicate_name_with_a_different_class_raises`
  - `test_registering_into_an_unknown_slot_raises`
  - `test_registering_into_a_planned_slot_raises_with_phase`
  - `test_params_must_be_a_frozen_dataclass_subclass`
  - `test_defaults_violating_their_own_bounds_fail_registration`
  - `test_unknown_policy_error_lists_registered_names`
  - `test_missing_requirements_are_reported` (`requires=("definitely_not_a_module_xyz",)`)
  - `test_isolated_registry_restores_global_state`
  - `test_discovery_finds_both_builtins_with_version_1`
- `tests/test_policy_framework_params.py`:
  - `test_defaults_fill_missing_keys`
  - `test_unknown_key_rejected_with_declared_list`
  - `test_bool_rejected_for_numbers`
  - `test_strings_are_not_coerced`
  - `test_int_accepted_for_float`
  - `test_ge_gt_le_lt_bounds` (parametrized)
  - `test_choices_enforced`
  - `test_non_finite_rejected`
  - `test_json_schema_shape_and_only_supported_keywords`
  - `test_to_dict_roundtrips_through_from_mapping`
- `tests/test_policy_catalog.py`:
  - `test_all_seven_slots_declared_with_kind_and_status`
  - `test_catalog_is_json_serialisable_and_has_schema_per_dispatch_policy`
  - `test_known_policies_command_keeps_datagen_keys_and_adds_slots`
  - `test_known_solvers_is_derived_from_the_registry`
  - `test_legacy_solver_registry_agrees_with_policy_registry`
- `tests/test_container_logistics_solver.py`: **replace** `test_get_solver_unknown_falls_back_to_default` with `test_get__solver_unknown_name_raises_listing_known` (assert `UnknownPolicyError`, with both names in the message). Add `test_get_solver_none_is_the_slot_default`.
- `tests/test_policy_fail_loud.py`:
  - `test_compile_rejects_a_typo_in_solver` (generate into a tmp `ORSIM_SCENARIOS_DIR`, expect `SpecValidationError`)
  - `test_run_start_refuses_unknown_strategy_in_profile`
  - `test_solver_override_unknown_name_raises`
- `tests/test_cli_no_hardcoded_policy_lists.py`: an AST check that `openride/config.py` assigns no `SOLVERS`/`POLICIES`, and that no `add_argument(..., choices=config.X)` remains in `openride/*.py`.

**P0 gate**
- The suite's FAILED/ERROR set equals the baseline (§6), with the one intended replacement.
- The golden test passes.
- All 16 specs compile, using a loop over `scenarios/*/spec.json` with `compile_spec_to_bundle` into tmp (see §6).
- A typo'd solver fails compile.

### P1: dispatch contract v2 (HARD GATE: golden byte-identical)

1. **`dispatch/plan.py`**
   ```python
   @dataclass(frozen=True)
   class Plan:
       chains: Mapping[str, tuple[str, ...]]    # truck_id -> ordered order_ids; insertion order = commit order
       notes: Mapping[str, Any] = field(default_factory=dict)
       @classmethod
       def from_pairs(cls, pairs: Iterable[tuple[dict, dict]], notes=None) -> "Plan"
           # str(_id) keys; raises DispatchContractViolation("truck 't1' appears twice in pairs")
       def heads(self) -> list[tuple[str, str]]
   ```
   An order that is deliberately deferred is simply absent from the plan.
2. **`dispatch/base.py`**: `class DispatchPolicy(ABC)`. Move these from `BaseAssignmentSolver` **verbatim**, including docstrings: `__init__(params: dict | None)`, `params` (raw dict), `set_rng`/`rng`, `set_tiebreak`/`tiebreak_seed`, `set_rebate_book`/`rebate_book`. Add `@abstractmethod decide(self, ctx) -> Plan`.

   **`dispatch/pair_solver.py`**: `class PairSolver(DispatchPolicy)` with abstract `solve(trucks, orders, *, pair_allowed, cost)` and the default adapter:
   ```python
   def decide(self, ctx):
       if ctx.default_candidate_kind == "spatial":
           return Plan.from_pairs(self.solve_pairs(ctx.candidate_pairs("spatial"), cost=ctx.accounting_cost))
       return Plan.from_pairs(self.solve(ctx.sampled_trucks(), list(ctx.raw_orders),
                                         pair_allowed=ctx.allowed_raw, cost=ctx.accounting_cost))
   ```
   **`assignment/solver/base.py`**: `BaseAssignmentSolver = PairSolver`, plus `Truck`, `Order`, `PairAllowed`, `Cost`, `Assignment` re-exported. Tests patch `BaseAssignmentSolver.set_tiebreak`, and that must still reach Greedy (MRO).
3. **`dispatch/helpers/candidates.py`**
   ```python
   CandidateKind = Literal["spatial", "sampled", "full"]
   @dataclass(frozen=True)
   class CandidateSettings:
       cell_deg: float; per_order: int; max_rings: int; max_trucks_per_scope: int
       @classmethod
       def from_profile(cls, prof) -> "CandidateSettings"   # EXACT parsing of app.py:543-553
       def to_dict(self) -> dict
   def spatial_pairs_for_partition(trucks, orders, *, rule: PartitionRule, settings) -> list[tuple[dict, dict]]
       # verbatim body of app.py:665-689 (orders grouped by order-haulier in first-seen order,
       # eligible = trucks with a haulier that rule.serves(...)), calling spatial.iter_candidate_pairs via the module
   def sampled_trucks(trucks, *, cap: int, rng) -> list[dict]
       # same as `bt = trucks; if len(bt) > cap: bt = rng.sample(bt, cap)`
   def full_pairs(trucks, orders, *, allowed) -> list[tuple[dict, dict]]
   ```
   **`dispatch/helpers/objective.py`**:
   - `deadhead_km(truck, order) -> float`: haversine from anchor to pickup, `inf` when unknown.
   - `is_dual_cycle(truck, order, *, radius_km) -> bool`: the exact predicate from `assignment_cost`. Factor it out of `constraints` as `constraints._is_dual_cycle_pair` and have `assignment_cost` call it, so the arithmetic is not duplicated.
   - `dual_cycle_cost(truck, order, *, bonus_km, radius_km) -> float`: `return constraints.assignment_cost(truck, order, {"dual_cycle_bonus_km": bonus_km, "dual_cycle_radius_km": radius_km})`.
4. **`dispatch/allowed.py`**: these must be verbatim moves.
   ```python
   @dataclass(frozen=True)
   class PartitionRule:
       max_travel_time_pickup: float | None; adjacency: Mapping[str, frozenset[str]]
       def allowed(self, t, o) -> bool    # == app.py:603-616
       def serves(self, truck_haulier, order_haulier) -> bool   # == app.py:647-650
       def to_dict(self) -> dict          # for capture
   @dataclass(frozen=True)
   class CompanyRule:
       haulier_id: str; max_travel_time_pickup: float | None
       def allowed(self, t, o) -> bool    # == pooled_planner.py:455-461 (own: pair_allowed; else pair_allowed_ignoring_haulier)
       def to_dict(self) -> dict
   ```
   app.py builds a `PartitionRule` once per tick. `pooled_planner` builds rules as `CompanyRule(carrier, ctx.max_travel_time_pickup).allowed`, used both in `_pair_allowed_for` and in the `build_round_candidates(pair_allowed_for=…)` factory. The `pair_allowed_own`/`pair_allowed_pooled` context fields stay, because `owner_reserve_km` uses them.
5. **`dispatch/views.py` and `dispatch/context.py`**
   ```python
   @dataclass(frozen=True)
   class TruckView: id: str; haulier_id: str | None; size: str; position: tuple[float, float] | None
       last_dropoff_position: tuple[float, float] | None; last_dropoff_facility: str | None; raw: dict
   @dataclass(frozen=True)
   class OrderView: id: str; haulier_id: str | None; size: str; pickup: tuple[float, float] | None
       dropoff: tuple[float, float] | None; pickup_facility: str | None; dropoff_facility: str | None; raw: dict
   @dataclass(frozen=True)
   class Scope: kind: Literal["partition","company"]; key: str; round: int | None = None
   @dataclass(frozen=True)
   class DispatchClock: sim_clock: str; time_step: int; horizon_steps: int | None
   class DispatchContext:
       scope; params (PolicyParams | None); clock; rng; tiebreak_seed; rebates; candidate_settings
       default_candidate_kind: str; raw_trucks: tuple[dict, ...]; raw_orders: tuple[dict, ...]
       accounting_cost: Callable[[dict, dict], float]
       trucks / orders  -> functools.cached_property, tuple of views (lazy; built-ins never touch them)
       def allowed(self, truck, order) -> bool      # accepts views or raw (unwraps .raw)
       allowed_raw: Callable[[dict, dict], bool]
       def candidate_pairs(self, kind: str | None = None) -> list[tuple[dict, dict]]  # cached per kind
       def sampled_trucks(self) -> list[dict]
       def deadhead_km(self, truck, order) -> float
       def dual_cycle_cost(self, truck, order, *, bonus_km, radius_km) -> float
   ```
   Values of the context fields:
   - `rng`: `policy.rng`, which is the `random` module on the partitioned path and the tick `Random` on the pooled path.
   - `tiebreak_seed`: `policy.tiebreak_seed`.
   - `rebates`: `policy.rebate_book`.
   - `ctx.travel`, `ctx.facilities`, `ctx.state` and `held` are **not** part of P1; they arrive with P4. Say so in the docstring.

   **`dispatch/scopes.py`** has `partition_context(...)` and `company_context(...)`:
   - Partition: the spatial source is `spatial_pairs_for_partition(...)`, and the sample RNG is the **`random` module**.
   - Company: the spatial source is `list(prebuilt_pairs or ())`, and the sample RNG is the tick `Random`.
   - Scope keys: `partition:haulier:<id>` / `partition:component:<n>` and `company:<hid>`, with the round number.
6. **`dispatch/profile.py`**
   ```python
   @dataclass(frozen=True)
   class ResolvedDispatch: name: str; version: str; timing: str; params: PolicyParams | None
       candidate_kind: str; source: Literal["policies","legacy-profile","unregistered"]
   def resolve_dispatch_for_profile(prof: Mapping) -> ResolvedDispatch
   def legacy_dispatch_from_profile(prof) -> dict   # {"type","timing":"online","params"}
   def build_dispatch_policy(resolved: ResolvedDispatch) -> DispatchPolicy   # cls(params=resolved.params.to_dict())
   def resolve_for_instance(policy, prof) -> ResolvedDispatch
   ```
   - **Legacy translation**: `type = strategy or default`. `params` = the keys of `solver_params` that the target `Params` declares; undeclared keys are dropped at DEBUG, because legacy keys were never validated. When `use_spatial_matching` is false, set `candidates="sampled"`.
   - **`resolve_for_instance`**: when `type(policy)` is registered and its name equals the profile's resolved name, use the resolved params. When it is registered but the name differs, raise `PolicyError("AssignmentApp._solver is a X but the profile resolves dispatch to Y")`. When it is unregistered, return `source="unregistered"`, `params=None`, and `candidate_kind = "spatial" if (prof.get("use_spatial_matching", True) and hasattr(policy,"solve_pairs")) else "sampled"` (the legacy rule).
   - **P2 profile shape**: a profile with a `policies` block plus `use_spatial_matching: false` raises (the key is superseded).
7. **`dispatch/invariants.py`**: `check_plan(plan, ctx) -> list[str]`. It reports:
   - a result that is not a `Plan`
   - a chain key that is not a truck id in scope
   - an empty chain
   - any order id appearing twice anywhere
   - an order id not in scope
   - a head pair where `ctx.allowed_raw(truck, order)` is false

   Time-feasibility of chain tails is P4.
8. **`dispatch/runner.py`**
   ```python
   class DispatchRunner:
       def __init__(self, *, policy: DispatchPolicy, resolved: ResolvedDispatch, capture=None)
       candidate_kind: str
       def partition_context(self, **kw) -> DispatchContext
       def company_context(self, **kw) -> DispatchContext
       def run(self, ctx) -> list[tuple[dict, dict]]
       stats: dict   # calls, violations, total_ms, max_ms; in memory only
   def build_runner_for_app(policy, prof) -> DispatchRunner
   ```
   `run()`:
   1. Call `plan = policy.decide(ctx)`. A `DispatchContractViolation` raised inside is caught and treated as a violation. Any **other exception propagates unchanged**, so today's exception behavior is preserved.
   2. Run `check_plan`. If it reports anything, log
      `logger.error("DISPATCH POLICY CONTRACT VIOLATION: %s v%s in scope %s at step %d returned an invalid plan: %s. The plan was DISCARDED (nothing committed for this scope this tick). This is a policy bug: run `openride policy validate dispatch %s`.", …)`,
      increment `violations` and return `[]`.
   3. Otherwise map the heads to `(raw_truck, raw_order)` in chain-insertion order. Timing `online` drops the tails.
   4. Log notes at DEBUG.
9. **Port the built-ins** with explicit, readable `decide()` methods:
   ```python
   # GreedyNearestSolver
   def decide(self, ctx):
       p = ctx.params
       cost = lambda t, o: ctx.dual_cycle_cost(t, o, bonus_km=p.dual_cycle_bonus_km, radius_km=p.dual_cycle_radius_km)
       if p.candidates == "spatial":
           return Plan.from_pairs(self.solve_pairs(ctx.candidate_pairs("spatial"), cost=cost))
       return Plan.from_pairs(self.solve(ctx.sampled_trucks(), list(ctx.raw_orders), pair_allowed=ctx.allowed_raw, cost=cost))
   # RandomAssignmentSolver
   def decide(self, ctx):
       if ctx.params.candidates == "spatial":
           return Plan.from_pairs(self.solve_pairs(ctx.candidate_pairs("spatial"), cost=ctx.accounting_cost))
       return Plan.from_pairs(self.solve(ctx.sampled_trucks(), list(ctx.raw_orders), pair_allowed=ctx.allowed_raw, cost=ctx.accounting_cost))
   def solve_pairs(self, candidate_pairs, *, cost):  # NEW, new behaviour only under candidates="spatial"
       # group candidates by str(order _id) in first-seen order; self.rng.shuffle(order_list);
       # per order: free candidates in pair order, self.rng.shuffle(cands), take [0]
   ```
   The default and legacy params give the same cost function as today's `_cost`, because `dual_cycle_cost` is `assignment_cost` with the same two floats. **Do not change `solve` or Greedy's `solve_pairs`.**
10. **`app.py` changes**
    - `__init__`: `self._solver = build_dispatch_policy(resolve_dispatch_for_profile(prof))`, wrapped as in P0.7. `_solver_params` is unchanged.
    - New method:
      ```python
      def _dispatch_runner(self, prof):
          from apps.container_logistics.policies.dispatch.runner import build_runner_for_app  # local import: cycle rule
      ```
      The cache key is `(id(self._solver), json.dumps(prof.get("policies"), sort_keys=True, default=str), prof.get("strategy"), json.dumps(prof.get("solver_params"), sort_keys=True), prof.get("use_spatial_matching", True))`. Store it in `getattr(self, "_dispatch_runner_cache", (None, None))`, mirroring `_market_components`.
    - Partitioned path: keep everything up to `_solve_bucket`. Build `rule = PartitionRule(max_pickup_travel, adjacency)` and use `rule.allowed` as `_pair_allowed`, still used for `benefit_km`. `_solve_bucket(bucket_trucks, bucket_orders, partition_key)` becomes `runner.run(runner.partition_context(...))`. The tpool call passes the key. The `_serves` logic moves into `rule.serves`. **Do not touch the benefit loop.**
    - Pooled path: keep `set_rng`/`set_tiebreak` exactly as they are. `PooledPlannerContext(dispatch=runner, max_travel_time_pickup=…, candidate_settings=…, clock=…, …)`, and remove the `solver` field.
11. **`pooled_planner.py`**
    - Replace both hasattr checks with `ctx.dispatch.candidate_kind == "spatial"`.
    - `_solve_company(trucks, orders, *, haulier_id, rnd, ctx, prebuilt_pairs=None)` keeps today's early returns in the same order: on the spatial path, `if not prebuilt_pairs: return []`; otherwise `if not trucks or not orders: return []`. Then `return ctx.dispatch.run(ctx.dispatch.company_context(...))`.
    - `build_round_candidates` stays put, keeps its signature, and is called by module-global name.

**P1 tests**
- `tests/test_dispatch_golden_identity.py` (from G0) passes unchanged. Add `test_golden_case_via_init[...]`.
- `tests/test_dispatch_plan_and_invariants.py`:
  - `test_from_pairs_preserves_pair_order`
  - `test_from_pairs_rejects_a_truck_twice`
  - `test_check_plan_flags_unknown_truck`, `…_unknown_order`, `…_order_twice`, `…_disallowed_head`, `…_empty_chain`, `…_non_plan_return`
  - `test_runner_discards_violating_plan_and_logs_error` (caplog, prefix asserted)
  - `test_runner_counts_violations`
  - `test_runner_online_timing_commits_heads_only`
  - `test_runner_propagates_non_contract_exceptions`
- `tests/test_dispatch_context_helpers.py`:
  - `test_spatial_partition_pairs_match_the_old_inline_grouping` (keep a frozen copy of the old inline loop in the test as the oracle)
  - `test_sampled_trucks_cap_and_rng_source` (module vs tick `Random`)
  - `test_dual_cycle_cost_equals_assignment_cost_on_a_grid`
  - `test_candidate_settings_from_profile_matches_app_parsing` (including clamps such as `per_order=0` → 1)
  - `test_views_expose_typed_fields_and_raw`
  - `test_partition_rule_matches_legacy_closure_on_a_pair_grid` and `test_company_rule_matches_legacy_closure_on_a_pair_grid` (frozen oracle copies)
- `tests/test_dispatch_profile_resolution.py`:
  - `test_legacy_profile_translates_strategy_params_and_use_spatial`
  - `test_random_drops_undeclared_legacy_dual_cycle_keys`
  - `test_unregistered_pair_solver_uses_the_legacy_kind_rule_and_default_decide`
  - `test_injected_solver_class_mismatch_raises`
  - `test_hauliers_harness_profile_without_strategy_resolves_to_random`
- `tests/test_random_spatial_candidates.py`:
  - `test_random_spatial_uses_only_candidate_pairs`
  - `test_random_spatial_never_double_assigns`
  - `test_random_spatial_reproducible_under_seeded_rng`
- `tests/test_policy_import_cycles.py`: `test_each_module_imports_cleanly_as_the_first_import[module]`. It runs `subprocess.run([sys.executable, "-c", f"import {m}"])` for every module under `apps.container_logistics.policies`, plus `assignment.app`, `assignment.solver` and `assignment.pooled_planner`.
- Every existing assignment, pooled and solver test passes **unedited**.

**P1 gate**
- The golden test is green (byte-identical).
- The subset in §6 is identical to the baseline.
- The import-cycle test is green.
- **Live:** after merging into the main tree, restarting celery, and with user approval, run `multi_haulier_greedy_solver_500_trucks` n=3, interleaved, plus `collab_scale_500t_v2` pooled n=3. Report `empty_ratio` together with `dual_cycle_rate`, inside the documented band. Check 100% `osrm` geometry and 0 `CONTRACT VIOLATION` lines (`journalctl --user -u openride-celery | grep -c "DISPATCH POLICY CONTRACT VIOLATION"`).

### P2: selection and access

1. **`preprocess.py::_normalize_policies(raw, planner_cfg) -> tuple[dict, dict]`.** It returns the authored block and its provenance, and is **shape only** (no registry).
   - `raw["policies"]` must be absent, or a dict of slot → `{type: str, timing?: str, params?: dict}`. Entries may contain only these keys.
   - When `policies.dispatch` is absent, translate: `type = raw.solver or planner.deployment.type or D strategy`; `params = {**deployment.params, **solverParams}`; `timing = "online"`; provenance `legacy`.
   - When it is present: a non-null `solver` that differs from `type` raises `SpecValidationError("spec sets solver='X' but policies.dispatch.type='Y'. 'solver' is the legacy spelling of policies.dispatch.type; send only policies.dispatch (the dashboard and CLI do).")`. A non-empty `solverParams` raises `"…solverParams is the legacy spelling of policies.dispatch.params; it cannot be combined with a policies block."`.
   - `planner.timing.mode` other than `online` raises `"planner.timing.mode='static' has never had a runtime reader; dispatch timing is now policies.dispatch.timing ('online'; 'plan' lands with P4)."`.
   - Mirrors: `profile.strategy = type`; `profile.solver_params = defaults ⊕ deployment.params ⊕ authored params` (the one-dial rule, kept so that accounting matches F6); `planner.deployment = {type, params: solver_params}`.
   - Recipe: `"policies": authored`, `"solver": type` (mirror), `"solverParams": None`.
   - Add `Compiled.policies_provenance: dict = field(default_factory=dict)`.
2. **`framework/resolve.py`**
   ```python
   @dataclass(frozen=True)
   class ResolvedPolicy: slot: str; name: str; version: str; kind: str; timing: str | None; params: dict[str, Any]
       def to_profile(self) -> dict   # {"type","version","kind","timing","params"}
   def resolve_policies_block(block: Mapping | None, *, where: str = "policies",
                              legacy_slots: Collection[str] = ()) -> dict[str, ResolvedPolicy]
   def authored_after_resolution(block, resolved, legacy_slots) -> dict   # drops undeclared legacy params (WARNING)
   def merge_policy_overrides(current: dict[str, ResolvedPolicy], overrides: Mapping) -> dict[str, ResolvedPolicy]
   def parse_policy_cli_args(policies: Sequence[str], params: Sequence[str]) -> dict
       # "dispatch=X" / "dispatch.k=v"; v = json.loads(v) if valid JSON else the raw string
   def policies_stamp(resolved, *, candidate_settings: dict, source: str) -> dict
   ```
   Errors:
   - Unknown slot: `"Unknown policy slot 'dispach' in 'policies'. Known slots: dispatch, market.offer, …"`.
   - Planned slot: `"Policy slot 'idle' is declared but not implemented yet (lands in P5). Remove 'policies.idle'."`.
   - Legacy slot: `"'policies.market.offer' is not selectable here yet; configure it via planner.market.offer (lands in P5)."`.
   - Timing `plan`: `"policies.dispatch.timing='plan' is defined but not executable yet: the held-plan dispatcher lands with P4. Use 'online'."`.
   - Bad timing: `"… is not a dispatch timing. Allowed: online, plan."`.
   - Missing dependencies: `"Dispatch policy 'X' requires module(s) not importable in this environment: ortools. Install them or pick another policy."`.

   Overrides: if the type changes, params reset to the new policy's defaults plus the override params. If the type is the same, merge. The result is re-validated.
3. **`spec_compile.py`**. After `Preprocessor.compile`:
   - `resolved = resolve_policies_block(recipe["policies"], legacy_slots=provenance)`; on `PolicyError`, raise `SpecValidationError`.
   - `recipe["policies"] = authored_after_resolution(...)`.
   - After generation, for every assignment behavior, set `b["profile"]["policies"] = {s: r.to_profile() for s, r in resolved.items()}` and assert `profile.strategy == resolved["dispatch"].name`.
   - The P0 name check is subsumed by this.
4. **Carry rules (`frontend_scenario_spec.py`)**
   - `SPEC_KEYS["policies"] = Carry.CARRY_IF_ABSENT`, inserted after `solverParams`.
   - `SPEC_KEY_NULL_POLICY["policies"] = NullPolicy.UNSUPPLIED` (comment: "a clear is `{}`, meaning all slot defaults; null is what a form serialiser emits").
   - `from_body["policies"] = deepcopy(raw.get("policies"))`.
   - New `drop_dispatch_dials_superseded_by(incoming: dict, carried: dict | None) -> dict | None`: when `incoming` supplies a non-null `policies`, return `carried` without `solver`/`solverParams`. It is asymmetric on purpose: a stale legacy client that sends only `solver` over a carried `policies` gets the loud "disagree" error instead of being silently ignored.
   - Call it in `assemble_spec` on `previous`, and in `edit_scenario` on `base` before the shallow merge.
   - Update the `solver` null-policy comment.
   - editForm: `"policySlots": recipe policies, or the legacy translation for old specs`, and `"solver": policySlots.dispatch.type` as raw text, no coercion.
5. **Per-run overrides**
   - `scenario_overrides.apply_policy_overrides(assignment_collection, overrides: Mapping) -> dict[str, ResolvedPolicy] | None` raises `PolicyError`. Current = profile `policies`, or the legacy translation. It merges, then patches each behavior: `profile["policies"]` (resolved) and the `profile["strategy"]` mirror. `solver_params` is not touched.
   - `apply_solver_override(coll, strategy)` = `apply_policy_overrides(coll, {"dispatch": {"type": strategy}})` and returns the name.
   - `run_container_logistics_simulation.main`: read `ORSIM_POLICY_OVERRIDES` (JSON) and `ORSIM_SOLVER`. If both name a dispatch type and they differ, raise `PolicyError("ORSIM_SOLVER='A' conflicts with ORSIM_POLICY_OVERRIDES dispatch.type='B'")`. Apply, then call `stamp = policies_stamp(...)` from the effective first profile with `CandidateSettings.from_profile`. Set `scenario_manager.orsim_settings["POLICIES"] = stamp`. The run name gets `· <dispatch name>` **only when the dispatch type was overridden** (unchanged behavior). Print `Policies effective: dispatch=GreedyNearest v1 timing=online params={...}`. Any `PolicyError` exits 2 before `SimulationRuntime`.
   - `scenario_manager.get_run_config_meta`: add `"policies": self.orsim_settings.get("POLICIES")`.

   Stamp shape:
   ```json
   {"dispatch":{"name":"GreedyNearest","version":"1","kind":"operator","timing":"online",
    "params":{"candidates":"spatial","dual_cycle_bonus_km":5.0,"dual_cycle_radius_km":0.5},
    "source":"bundle|legacy-profile|override",
    "candidate_settings":{"cell_deg":0.02,"per_order":40,"max_rings":8,"max_trucks_per_scope":500}}}
   ```
6. **Control plane and CLI**
   - `command.py run-simulation` gains `--policy` (repeatable), `--policy-param` (repeatable) and `--capture-dispatch` (P3).
   - **Validate before spawning.** Use `openride_control/policies.py::validate_run_policy_args(solver, policies, params) -> dict | str`: dry-run the override against the scenario's assignment profile (`ScenarioManager` load, or the legacy translation). On error, emit `{"ok": false, "action": "run_simulation", "message": …}` and exit 1 without spawning.
   - `manager.run_simulation_background(..., policy_overrides: dict | None)` sets `ORSIM_POLICY_OVERRIDES`.
   - `openride run` and `openride/runner.py`: add the same flags; `--solver X` becomes sugar for `--policy dispatch=X`.
   - `scenario new/edit`: `--solver X` is sent as `spec["policies"] = {"dispatch": {"type": X}}`; add `--policy` and `--policy-param` the same way.
   - `openride/ui.py` shows `policies.dispatch`.
7. **Dashboard (FE)**
   - `route.ts` (policies): return `slots: result.slots ?? null`.
   - `lib/policy-slots.ts`:
     ```ts
     export type ParamSchema = { type: 'number'|'integer'|'boolean'|'string'; default?: unknown; minimum?: number;
       exclusiveMinimum?: number; maximum?: number; exclusiveMaximum?: number; enum?: string[]; description?: string };
     export type PolicyEntry = { name: string; version: string; description: string; available: boolean; missing: string[];
       params_schema: { properties: Record<string, ParamSchema> }; defaults: Record<string, unknown> };
     export type SlotEntry = { kind: 'operator'|'world'; status: 'implemented'|'legacy'|'planned'; default?: string;
       timings?: string[]; executable_timings?: string[]; policies?: PolicyEntry[]; lands_in?: string };
     export type PolicyCatalog = Record<string, SlotEntry>;
     export type SlotSelection = { type: string; timing?: string; params: Record<string, unknown> };
     export type PolicySelection = Record<string, SlotSelection>;
     export function fieldKind(s: ParamSchema): 'number'|'integer'|'boolean'|'enum'|'string'
     export function validateParamValues(schema, values): Record<string, string>   // same subset as python
     export function defaultSelection(catalog): PolicySelection
     export function selectionFromEditForm(form: {policySlots?: …; solver?: string}, catalog): PolicySelection
     export function withPolicyType(sel, slot, name, catalog): PolicySelection   // type change -> defaults
     export function selectionForApi(sel): { [slot: string]: { type: string; timing?: string; params: Record<string, unknown> } }
     ```
   - `PolicySlotsEditor.tsx`: one dropdown per **implemented** slot, in an "Operator" group. A policy with `available=false` is a disabled `<option>` with the reason. Dispatch gets a timing select with `plan` disabled and labelled "lands with P4". Below sits a single muted line listing the planned and legacy slots. If `slots` is missing from the API, render the error "backend does not expose the policy catalog (known-policies.slots); is openride_apps on feat/policy-slots?" and disable saving. **Never fall back to a hardcoded list.**
   - `PolicyParamsForm.tsx`: a generic form generated from the schema (`NumberField` for number/integer with min/max, checkbox, enum select, text input). Help text and inline errors come from `validateParamValues`.
   - `ScenarioTab.tsx`: replace the "Assignment solver" card. Add state `policySlots`. Hydrate from `form.policySlots`, falling back to `form.solver`. The body sends `policies: selectionForApi(policySlots)` and **no `solver`**. Save is blocked while there are param errors. The snapshot includes `policySlots`.
   - Delete `lib/scenario-solvers.ts`.
   - `scenario-world.ts`: add `'policies'` to `NON_WORLD_KEYS`, with a comment: "operator slots only; P5 world slots must NOT be stripped".
   - `runNameMeta.ts`: rename the list to `LEGACY_DISPATCH_NAMES` and make it `parseRunNameTags(name, knownDispatch: readonly string[] = LEGACY_DISPATCH_NAMES)`.
   - `scenario/run/route.ts`: drop the `KNOWN_SOLVERS` check and forward `solver` as is. The python side validates before spawning.

**P2 tests**
- `tests/test_policies_spec_block.py`:
  - `test_legacy_solver_translates_to_policies_dispatch`
  - `test_planner_deployment_translates_when_no_solver`
  - `test_policies_block_wins_and_is_mirrored_into_strategy_and_deployment`
  - `test_solver_disagreeing_with_policies_dispatch_is_rejected`
  - `test_solverparams_with_policies_block_is_rejected`
  - `test_unknown_slot_rejected`, `test_planned_slot_rejected_with_phase`, `test_legacy_market_slot_points_to_planner_market`
  - `test_unknown_entry_key_rejected`, `test_plan_timing_rejected_with_p4_message`, `test_planner_timing_static_rejected_with_translation`
  - `test_unknown_param_rejected_in_policies_block`, `test_undeclared_param_dropped_with_warning_in_legacy_translation`
  - `test_bundle_profile_carries_resolved_policies_with_version_and_defaults`
  - `test_recipe_echoes_authored_not_resolved`
  - `test_policies_block_never_perturbs_generated_agents`: compile the same spec with and without an equivalent `policies` block; trucks, orders and facilities JSON are byte-equal. This is invariant I1.
  - `test_every_shipped_spec_translates_to_its_current_solver`: shape-only, over `scenarios/*/spec.json`.
- `tests/test_policies_spec_carry.py`:
  - `test_body_with_policies_does_not_carry_legacy_solver`
  - `test_body_without_policies_carries_policies`
  - `test_stale_legacy_client_gets_a_loud_disagreement`
  - `test_edit_with_policies_patch_over_legacy_spec_compiles` (tmp `ORSIM_SCENARIOS_DIR`)
  - `test_policies_null_is_unsupplied`
  - **Edit** `tests/test_assemble_spec_key_registry.py`: add the `policies` sentinel `{"dispatch":{"type":"GreedyNearest","timing":"online","params":{"dual_cycle_bonus_km":3.25}}}`. This is the only edit there, and it is forced by its own guard.
- `tests/test_policy_run_overrides.py`:
  - `test_parse_policy_cli_args_json_coercion`
  - `test_type_change_resets_params_to_new_defaults`, `test_same_type_merges_params`
  - `test_invalid_override_param_raises`
  - `test_orsim_solver_still_works`, `test_orsim_solver_conflicting_with_policy_override_raises`
  - `test_override_patches_profile_policies_and_strategy_mirror`
  - `test_stamp_shape`, `test_run_config_meta_mirrors_policies`
  - `test_run_simulation_command_rejects_bad_policy_without_spawning` (monkeypatch `ServiceManager.run_simulation_background` to assert it is not called)
- `tests/test_policy_param_changes_behaviour.py`: `test_dual_cycle_bonus_param_changes_awards_end_to_end`. On a fixture where a 3 km dual-cycle truck competes with a 0.5 km truck, the profile `policies.dispatch.params.dual_cycle_bonus_km` of 5.0 versus 0.0 gives different award sets. That proves the editor does something.
- `tests/test_frontend_editform_policy_slots.py`: editForm has `policySlots` and a `solver` mirror. `tests/test_scenario_index.py` still passes.
- FE `lib/policy-slots.test.ts`:
  - field kinds; bounds, exclusive bounds, integer and enum validation
  - `selectionForApi` shape; a type change resets params; `selectionFromEditForm` falls back to `solver`
  - an unavailable policy is reported as disabled
- FE: `scenario-world.test.ts` adds "policies is non-world"; `runNameMeta.test.ts` adds "an extra dispatch name is classified as solver, not structure".

**P2 gate**
- pytest: the subset plus the new tests are green, and the FAILED set equals the baseline.
- `/verify-frontend`: `tsc --noEmit` errors equal the baseline; vitest shows the baseline 485 plus the new tests, all passing.
- **Offline end to end:** POST to the control `generate-scenario` with `policies.dispatch.params.dual_cycle_bonus_km=0`, then `spec.json` and `scenario.json` profile show it, then the stamp function shows it, then the behaviour test differs.
- **Browser and live:** see §6.

### P3: validate, bench, CLI policy group

1. **`testing/fixtures.py`**: deterministic synthetic contexts built with `scopes.partition_context`: `empty_trucks`, `empty_orders`, `single_truck`, `no_geometry`, `one_haulier`, `all_infeasible` (rule with `max_travel_time_pickup=1`), `tied_costs`, and `scale(n_trucks=1000, n_orders=500, hauliers=1, seed)`.
2. **`testing/battery.py`**: `CheckResult(group, check, ok, detail, ms)`. `run_battery(slot, name) -> list[CheckResult]` instantiates a fresh policy per check.
   - Structure: `returns_plan`, `no_truck_twice`, `no_order_twice`, `only_in_scope`, `only_allowed_pairs`.
   - Robustness: one check per fixture; no exception and a valid plan.
   - Determinism: `reproducible` (two fresh instances, same `Random(7)` seed, same tiebreak, same plan); `order_independent` (with a tiebreak seed set, 20 shuffles of trucks and orders give one plan; skipped with `ok=True, detail="order_sensitive: <reason>"` when declared).
   - Performance: `scale` under `spec.perf_budget_ms`, best of 3.
   - Params: `defaults_validate`, `rejects_unknown_key`, `rejects_out_of_bounds` (per bounded field), `rejects_bad_choice`, `schema_keywords_supported`.
3. **Performance budgets.** Measure Greedy and Random on `scale` five times on a quiet box. Set `perf_budget_ms = max(3 × worst, 500)` and write the measured numbers in a comment at the decorator (CLAUDE.md §17: this box varies 1.3-2×).
4. **`dispatch/capture.py`**
   - `CaptureSettings(dir: str, every_n_ticks: int, max_files: int)` comes from `profile["policy_capture"]`, and is absent by default (a single `if`, zero cost).
   - `context_to_fixture(ctx, plan, policy_spec)` produces:
     ```
     {"format":"openride.dispatch_context/1","run_id","time_step","sim_clock",
      "scope":{"kind","key","round"},"policy":{"name","version","params"},
      "candidate_settings":{…},"rule":rule.to_dict(),"tiebreak_seed","trucks":[raw],"orders":[raw],
      "spatial_pairs":[[tid,oid],…] | null,"live_heads":[[tid,oid],…]}
     ```
     The file is `<dir>/step_<n>_<scope-key>.json.gz`, with `mtime=0`.
   - `context_from_fixture(fx) -> DispatchContext`. `spatial_pairs` is set for company scope; for partition scope the pairs are recomputed.
   - The runner calls `maybe_capture` after `decide` and before the invariants are applied, and logs if writing fails without blocking the tick.
   - Sim main maps `ORSIM_CAPTURE_DISPATCH="every=N,max=M"` to `profile["policy_capture"] = {dir: <datahub>/fixtures/dispatch/<run_id>, …}`. `openride run --capture-dispatch every=10,max=40` sets it.
5. **`testing/bench.py`**: `bench(names, fixtures, param_overrides) -> BenchReport` and `render_table(report) -> str`.
   - Each (fixture, policy) gets a fresh instance with `set_rng(Random(fixture seed or 0))` and `set_tiebreak(tiebreak_seed)`, then `runner.run`.
   - Per row: `policy`, `fixtures`, `assigned`, `orders`, `assign_rate`, `deadhead_km_total`, `deadhead_km_mean`, `dual_cycle`, `solve_ms_p50`, `solve_ms_max`, `violations`.
   - The header must say: "haversine deadhead (decision-time proxy, NOT the road-distance KPI); single-tick scores screen policies, runs certify them (n≥3 interleaved)."
6. **Control commands** in `openride_control/policies.py`, wired in `command.py`:
   - `policy-validate --slot dispatch --name X` returns `{ok, results:[CheckResult…]}`, with ok false if any check failed.
   - `policy-bench --slot dispatch --policies A,B (--fixtures DIR | --synthetic scale) [--policy-param A.k=v]` returns `{ok, report, table}`.
   - `control.py` gets `known_policy_slots()`, `policy_validate()` and `policy_bench()`.
7. **CLI**: new `openride/policy_cmd.py`, registered in `cli.py`:
   - `policy list [slot] [--json]`: marks defaults and missing dependencies, and shows legacy and planned slots muted.
   - `policy show SLOT NAME [--json]`: description, version, a params table (type, default, bounds, help).
   - `policy validate SLOT NAME`: PASS/FAIL table; exits 1 on any FAIL.
   - `policy bench SLOT --policies A,B --fixtures DIR|--synthetic scale [--json]`.
   - `openride solver list` stays as an alias that renders `policy list dispatch`.
8. **Golden versions.** `tests/golden/policy_golden.json.gz` holds, per dispatch policy, `{version, cases:[synthetic fixture id, expected heads]}`. The generator is `tests/golden/capture_policy_golden.py --policy NAME`. Tests:
   - `test_every_dispatch_policy_has_a_golden_entry`
   - `test_policy_output_unchanged_or_version_bumped[name]`, failing with `"GreedyNearest v1 output changed on fixture X but its version did not: bump version= in @policy and regenerate with tests/golden/capture_policy_golden.py --policy GreedyNearest"`
9. **`docs/policy_authoring.md`**, one page: write (a file in `policies/dispatch/library/`, the decorator, nested `Params`), heavy dependencies imported inside `decide`, the context API and its P1 limits, `Plan` semantics, invariants, determinism (use `ctx.rng`, never module `random` directly), validate, bench, select (spec, dashboard, CLI), and the restart rule.

**P3 tests**
- `tests/test_policy_contract_battery.py`: `test_builtin_dispatch_policy_passes[GreedyNearest|RandomAssignment-<check>]`, parametrized over `list_policies("dispatch") × DISPATCH_CHECKS`.
- `tests/test_policy_battery_catches_broken_policies.py`, run under `isolated_registry()`:
  - `TwiceAssigner` (the same order in two chains) fails **only** `structure.no_order_twice`.
  - `DisallowedPairPolicy` fails only `structure.only_allowed_pairs`.
  - `SlowPolicy` (sleeps budget + 200 ms) fails only `performance.scale`.
  - `CrashOnEmptyPolicy` fails only the `robustness.empty_*` checks.
  - `ClockSeededPolicy` (uses `time.time_ns()` as RNG) fails `determinism.reproducible`.
  - `test_registering_bad_param_defaults_raises`.
- `tests/test_policy_golden_versions.py` (item 8).
- `tests/test_dispatch_capture_and_bench.py`:
  - `test_capture_writes_fixture_when_enabled`, `test_capture_off_by_default_writes_nothing`
  - `test_replaying_a_captured_context_reproduces_its_live_heads` (partition and company scope)
  - `test_bench_reports_both_policies_on_synthetic`, `test_bench_counts_violations`
- `tests/test_cli_policy_group.py`: AST check that `policy` has list/show/validate/bench; `test_solver_list_is_an_alias`; the control wrappers parse the payload.

**P3 gate**
- `openride policy validate dispatch GreedyNearest` and `… RandomAssignment` both print PASS.
- The broken-policy tests fail exactly the expected checks.
- `openride policy bench dispatch --policies GreedyNearest,RandomAssignment --synthetic scale` prints a table.
- After merge: one `--capture-dispatch` run, then bench over the captured directory.

---

## 5. Restart and rebuild implications, and comparability

**Restarts and rebuilds**

| Phase | Celery restart | Other |
|---|---|---|
| P0 | **Yes** (`assignment/solver/*` and `app.py` are imported by agents). | Control plane and CLI need nothing (fresh subprocess). |
| P1 | **Yes.** | |
| P2 | **Yes** for agent-side profile resolution. | Sim runner and control plane need nothing. A **scenario recompile is optional**: old bundles run through the legacy translation. Recompiling adds `policies` to `spec.json` and `scenario.json`; generated agents stay byte-identical (I1 test). Any dashboard or CLI edit recompiles anyway. **No api container rebuild**: `run_config.meta` is an unschema'd dict and there is no new Eve resource. Dashboard: `next dev` HMR plus a hard refresh; run `/verify-frontend` for staleness. |
| P3 | **Yes** (capture hook). | bench and validate need nothing. |

Adding or editing a policy file needs one celery restart. Choosing a policy or changing its params never does, because that ships as data at spawn. The `/restart-for` table (`~/.claude/skills/restart-for/SKILL.md:16`) should gain `apps/container_logistics/policies/**`. That file is outside the repo, so **ask the user** before editing it.

**Coordination (F20).** Celery and the dashboard run from `/home/user/openride_apps`. Every live gate (500-truck n=3, capture run, browser against real data) needs this branch merged into the main tree, which has uncommitted Kafka-transport edits to `assignment/app.py` (around `assign()` and `publish()`) and `run_container_logistics_simulation.py`. Keep P1 and P2 hunks away from the top of `assign()` and from `publish()` to minimise conflicts. **This merge is a user decision.**

For a worktree-only browser check: create a scratch workspace dir with symlinks `openride_apps -> /home/user/openride_apps_policy_slots` and `openride_server -> /home/user/openride_server`. Running a second `next dev` in the same project directory clashes on `.next`, so the alternative is to briefly restart `openride-analytics` with `OPENRIDE_WORKSPACE_ROOT` pointing at that scratch dir. That also needs user consent.

**Comparability statement**
- With every slot at its default, the change is **byte-identical** (golden: assignments, order, share tags, wire payloads, round stats, module-RNG consumption). Runs before and after are comparable.
- `RandomAssignment` with `candidates=spatial` is a **new arm**. It is not comparable with Random/sampled runs; that difference is the experiment. The arm is stamped in `run_config.meta.policies`.
- The new stamp fields (`policies`, `candidate_settings`) add provenance only.
- Changing the spatial knobs or `max_trucks_per_haulier` was already outcome-changing (audit P3) and is now visible in the stamp.
- World slots are not implemented, so no world boundary is crossed.

---

## 6. Acceptance gates and the final gate list

**Baselines** (measured today in the worktree):
- `venv/bin/python -m pytest -q --co` collects **1736 tests and 8 collection errors** (ride-hail, pre-existing). Pytest **aborts** on those errors unless you pass `--continue-on-collection-errors`.
- Assignment subset:
  ```
  venv/bin/python -m pytest -q -p no:cacheprovider tests/test_container_logistics_solver.py tests/test_assignment_*.py tests/test_scenario_market_overrides.py tests/test_spec_key_null_policy.py tests/test_datagen_policies.py tests/test_frontend_scenario_pools_roundtrip.py
  ```
  gives **240 passed, 1 failed**. The failure is `test_rebate_aware_flag_defaults_false_in_every_shipped_scenario`, pre-existing because the worktree has no compiled bundles.
- FE vitest: **42 files, 485 tests passing**.

**Step 0 for the coder:** record the full-suite FAILED/ERROR set and the tsc baseline, and save both to the scratchpad:
```
venv/bin/python -m pytest -q -p no:cacheprovider --continue-on-collection-errors -rfE 2>&1 | grep -E '^(FAILED|ERROR)' | sort > baseline_failures.txt
cd FE && npx tsc --noEmit 2>&1 | tail -3
```
Do **not** use `-x` for the full suite.

**Gates per phase:** see the end of each phase in §4.

**Final gate list**
1. `pytest tests/test_dispatch_golden_identity.py -q`: all cases pass, and the fixture was not modified after commit #1 (`git log --follow tests/golden/dispatch_golden_v1.json.gz` shows one commit).
2. Full-suite FAILED/ERROR set equals the baseline, except that `test_get_solver_unknown_falls_back_to_default` is replaced. The pass count goes up by the new tests and nothing else changes.
3. `pytest tests/test_policy_import_cycles.py tests/test_policy_*.py tests/test_dispatch_*.py -q`: green.
4. All 16 specs compile into a tmp root:
   ```
   ORSIM_SCENARIOS_DIR=$(mktemp -d) venv/bin/python -c "import glob,json,tempfile,os;from apps.container_logistics.scenario.spec_compile import compile_spec_to_bundle as c;[c(json.load(open(p)),domain='container-logistics-sim',scenario_dir=tempfile.mkdtemp(),slug=json.load(open(p))['slug']) for p in sorted(glob.glob('scenarios/*/spec.json'))];print('ok')"
   ```
   The result must be `ok`, and each bundle's dispatch must equal its old `solver`.
5. `venv/bin/python -m openride.cli policy list`, `policy show dispatch GreedyNearest`, `policy validate dispatch GreedyNearest` (PASS), `policy validate dispatch RandomAssignment` (PASS), `policy bench dispatch --policies GreedyNearest,RandomAssignment --synthetic scale`, and `solver list` (alias) all work.
6. `venv/bin/python -m openride_control.command known-policies` returns JSON with `policies`, `defaults` **and** `slots`.
7. `grep -rn "SOLVERS\|KNOWN_SOLVERS\|scenario-solvers" openride apps/container_logistics FE/lib FE/app FE/components` shows no hardcoded lists (only `LEGACY_DISPATCH_NAMES` and the legacy `SOLVER_REGISTRY` view).
8. FE: `npx tsc --noEmit` errors equal the baseline; `npx vitest run` shows 485 plus the new tests passing; run `/verify-frontend`.
9. **After merge, with user approval:**
   - `systemctl --user restart openride-celery`.
   - `/gate-run multi_haulier_greedy_solver_500_trucks` n=3 interleaved, and `collab_scale_500t_v2` with `--structure chain` pooled n=3. Check `empty_ratio` and `dual_cycle_rate` in band, 100% `osrm` geometry, 0 contract violations in journalctl, `run_config.meta.policies` present, and the run name unchanged on the default path.
   - Browser check (Playwright): edit `dual_cycle_bonus_km` in the Policies form, save, confirm `spec.json` and the bundle profile show it, run, confirm `run_config.meta.policies.dispatch.params` shows it.
   - One `--capture-dispatch` run, then `policy bench` over its fixtures.
10. Docs: `docs/policy_authoring.md` in the repo. Outside the repo: a new `/home/user/docs/reference/features/26-policy-slots.md`, a CLAUDE.md §5 pointer, and a doc 11 update (an unknown solver override now fails loud).

---

## 7. Risks, traps, and where the adversarial reviewer should look hardest

1. **Module-RNG consumption order.** On the partitioned path, sampling and the solver shuffles share the `random` module, so one extra or missing call shifts every later draw in the worker, including trucks' `random() <= response_rate`. The pooled early returns (no trucks or no orders, empty prebuilt pairs) must stay ahead of any decide call. The golden `rng_probe` catches this; the reviewer should still read `_solve_company` and `sampled_trucks` line by line.
2. **Import cycles (F19).** These are order-dependent and only show up when some module is the *first* import. The subprocess test covers it. The reviewer should check that no top-level `assignment.*` import has crept into a leaf module and that `app.py` imports `policies` only inside its accessor.
3. **Monkeypatch-sensitive names (F18).** A `from spatial import iter_candidate_pairs` would silently make `test_assignment_candidate_parity` vacuous rather than failing it. Check that its counts still change when the generator is wrapped.
4. **The `__new__` harnesses.** Any new attribute read in `assign()` without `getattr` breaks four test modules. A *mismatch-tolerant* resolution would hide wiring bugs; it should raise.
5. **Linked dials and carry.** The supersede helper, `edit_scenario`'s shallow merge, and the asymmetric rule (a stale legacy client gets a loud error). Try these: a dashboard save over a legacy `spec.json`; CLI `edit --solver`; a hand edit of `planner.deployment.type` (silently superseded, exactly as today, but say so in the docs).
6. **Golden vacuity.** Check the coverage matrix, and that the cap-40 cases really sample, the chain cases really share, and tick T is all ties. Also check that the fixture header's capture sha is `ec085f5`, and re-run the capture there.
7. **Discard on violation.** Inputs with duplicate ids used to double-assign silently under Random. Now the whole scope's plan is dropped. That is a deliberate fix, reachable only with corrupt input. The reviewer should confirm the golden fixtures contain no duplicate ids.
8. **Accounting vs policy objective.** The two coincide only through the one-dial mirror into `solver_params`. If a future policy defines a different objective, `benefit_km` and the arbitration still use the framework's `assignment_cost`. That is intended but must be documented (audit P5 is still open).
9. **Fail-loud flips.** `get_solver`, `apply_solver_override` and the compile check now raise. Look for callers that relied on the fallback: the legacy `normalize_solver` paths and old scripts. The offer, claim and arbitration registries remain fail-soft.
10. **Params pitfalls.** `bool` is an `int` subclass; JSON `5` for a float field; non-finite values; the CLI JSON coercion of `"true"` and `"60"`. The TS validator must accept exactly the same subset (the schema-keyword test pins it).
11. **`NON_WORLD_KEYS`.** Adding `policies` is correct only while every slot is an operator slot. P5 must split it.
12. **Performance budget flakiness.** The battery's performance check on this box varies 1.3-2×. Keep the 3× margin and a best-of-3.
13. **Merge zones with the main-tree transport work** (app.py `assign()` top and `publish()`, the sim main overrides block).
14. **Things the reviewer should not accept:** a golden fixture regenerated after commit #1; any edited existing test beyond the two named (the `get_solver` fallback test and the `assemble_spec` sentinel); a `try/except` that swallows `PolicyError` at run start; a hardcoded policy list in the frontend fallback.

---

## 8. How P4 and P5 plug in later

**P4: NetworkFlow and plan timing**
- Add `policies/dispatch/library/network_flow.py` with `@policy(slot="dispatch", name="NetworkFlow", requires=("ortools",), perf_budget_ms=…)`. Vendor `/home/user/solvers/dispatch_solvers/standalone` into `policies/dispatch/library/vendor/dispatch_core/`.
- `decide(ctx)` builds per-truck source nodes from `ctx.trucks` (views) and jobs from `ctx.orders`. It calls `ctx.allowed` for feasibility and returns `Plan(chains=…)`, mapping the library's per-vehicle tours to chains. Heavy imports go inside `decide`.
- P4 adds to the context: `ctx.travel` (a run-scoped OSRM facility table), `ctx.facilities` and `ctx.state`. It adds `time_budget_ms` with the slot's declared fallback (default GreedyNearest) plus a fallback counter, an execution mode (`thread` via `eventlet.tpool`; `process` is opt-in later), and one provenance writer for `meta.policy_runtime` (notes, violations, fallbacks), patched the way `meta.market` is.
- `timing: "plan"` becomes executable: the runner keeps `held` tails per scope, hands a freed truck its next held job without calling the solver, triggers re-planning, and held jobs reserve trucks. Flip `executable_timings` to include `"plan"`. The validation error disappears on its own.
- The scope stays a framework decision: one call per partition or per company.

**P5: the other slots**
- Each slot gets a base class (`QueuePolicy`, `IdlePolicy`, `SheddingPolicy`, `DurationPolicy`), a context, invariants, a default policy that reproduces today, and a golden test.
- Flip the slot's status to `implemented`. The registry, resolver, catalog, form and stamp work unchanged.
- `queue` replaces the `popleft` in `facility_queue_sm.py:68`. `idle` needs a truck movement path (the precedent is ridehail's `action_when_free`). The world slots carry `kind="world"`: the UI marks them as comparability boundaries and `NON_WORLD_KEYS` must stop stripping them.
- `market.offer` and `market.claim` move from `planner.market.*` into `policies.market.*`, with adapters over the existing classes and a deliberate decision on fail-loud vs doc 21 §7.
- Retire the dead keys (`idle_strategy`, `fifo_queue_policy`, `planner.timing`) with translation errors, as was done for `rebate_by_code`.
- Per-company policy config: the `policies` block can later accept `{"dispatch": {"type", "params", "per_haulier": {hid: {...}}}}`. The resolver already returns a resolved policy per slot, so the extension is one more level of lookup in `company_context`.

---

### Critical files for implementation
- /home/user/openride_apps_policy_slots/apps/container_logistics/assignment/app.py
- /home/user/openride_apps_policy_slots/apps/container_logistics/assignment/pooled_planner.py
- /home/user/openride_apps_policy_slots/apps/container_logistics/assignment/solver/ (`__init__.py`, `base.py`, `greedy_nearest.py`, `random_assignment.py`)
- /home/user/openride_apps_policy_slots/apps/container_logistics/datagen/preprocess.py and `apps/container_logistics/scenario/{spec_compile.py, frontend_scenario_spec.py, scenario_overrides.py}`
- /home/user/openride_apps_policy_slots/apps/simulation/run_container_logistics_simulation.py, and FE `/home/user/openride_server/analytics/components/scenario/ScenarioTab.tsx`

---

## Deviations (recorded by the coder, 2026-10-21)

Each item says what differs from the text above and why. Anything not listed was
implemented as written.

### G0 (golden capture)
1. **Dual-cycle trucks also stand at their drop-off.** The 30% of trucks that get
   `last_dropoff_loc`/`last_dropoff_facility_name` also get `current_loc` set to that
   point. Without it the anchor stayed at `init_loc` and zero-cost dual-cycle ties
   almost never occurred, which would have made the tie checks vacuous.
2. **Grid is 68 cases, with three intentional gaps recorded in the fixture header:**
   RandomAssignment × `use_spatial_matching=false` (a no-op on ec085f5, Random had no
   `solve_pairs`), world C × `chain` (that spec compiles no chain structure), and the
   tied world × cap 40 (4 trucks cannot bind a cap).
3. Capture and replay feed the app **sorted-key copies** of tick states and profiles,
   because the fixture is written with `sort_keys` and key order shows up in the
   published JSON strings.

### P0
4. Framework strictness beyond the text: registering one class under two names, or the
   same class with different metadata, raises; registering into a `legacy` slot raises
   naming its `configured_via` key; `get_policy_spec` on a legacy/planned slot raises
   `SlotNotImplementedError`; `default_policy_name` checks the default is registered;
   `isolated_registry()` runs discovery before its snapshot (otherwise rolling back a
   first discovery would drop the built-ins for the life of the process).
5. `SlotSpec` has a `configured_via` field; `market.claim` gets
   `configured_via="planner.market.claim"` (the table left its notes empty).
6. The sim main also wraps `apply_solver_override` so an unknown `ORSIM_SOLVER` exits 2
   with the run-start message instead of a traceback.
7. `app.py` imports `PolicyError` from the leaf framework at module top (safe: the
   framework never imports `assignment.*`) for the stale-worker hint.

### P1
8. **`ResolvedDispatch` lives in its own leaf module `dispatch/resolved.py`**, so
   `profile.py` and `runner.py` do not import each other.
9. **`DispatchContext` takes a lazy `spatial_source` callable and a separate
   `sample_rng`** (plus `rule`, kept for capture). Laziness guarantees spatial pairs are
   never built on the sampled path and the sample draws at most once, on demand.
10. **`company_context` takes a `CompanyRule` object.** `pooled_planner` obtains it via
    `DispatchRunner.company_rule(...)` (it imports nothing from `policies`) and caches one
    rule per carrier per tick, because `build_round_candidates` asks once per pair.
11. **More than one local import in `app.py`.** Rule 7 asks for exactly one accessor, but
    the plan also has `app.py` build the `PartitionRule` and the clock. There are local
    imports in `_build_dispatch_policy` (used by `__init__`), `_dispatch_runner`, the
    partitioned section of `assign()` (`PartitionRule`, `DispatchClock`) and
    `_assign_pooled` (`DispatchClock`), each commented with the cycle rule. Candidate
    settings come through `runner.candidate_settings(prof)` to avoid a fifth.
12. `PooledPlannerContext` dropped `solver`, `spatial_params`, `use_spatial` and
    `max_trucks_per_haulier`; it gained `dispatch`, `max_travel_time_pickup`,
    `candidate_settings`, `clock`. `_assign_pooled` gained a `sim_clock` argument. No test
    constructed the context directly.
13. `app._DEFAULT_MAX_TRUCKS_PER_HAULIER` and its rationale moved to
    `helpers/candidates.py` as `DEFAULT_MAX_TRUCKS_PER_SCOPE` (the only reader is now
    `CandidateSettings.from_profile`). The five dead parsing lines at the top of
    `assign()` were removed; a malformed spatial knob now raises a few lines later, inside
    the same `assign()` call.
14. **`OrderView` fields are `pickup_facility_name`/`dropoff_facility_name`**, not
    `pickup_facility`/`dropoff_facility`: the bare names are the order-embedded facility
    snapshot, which `tests/test_order_service_time_path.py` keeps reader-free.
15. `AssignmentApp.__init__` builds the policy with its *validated* params dict, so
    `_solver.params` differs from the `get_solver` harness path (raw `solver_params`).
    No built-in reads `self.params` (they read `ctx.params`); outputs are identical
    (golden via `__init__` passes).
16. Resolution is stricter than the text: `use_spatial_matching` must be a real bool
    (the pre-v2 code used `bool(...)`, so `0`/`None`/`""` silently picked an arm); an
    undeclared timing is refused as "not a dispatch timing" rather than "not executable";
    an unregistered subclass of a registered policy is refused (its inherited `decide`
    would read params it does not have); a policy that declares no `candidates` param
    gets candidate kind `sampled`; the run-start check also refuses a non-executable
    timing. `check_plan` additionally flags a chain that is not a tuple.

### P2
17. **`solverParams` next to a `policies` block is refused only when it disagrees** with
    `policies.dispatch.params`. The planned key-registry sentinel carries both spellings
    with equal values through an end-to-end compile; agreeing values are tolerated,
    symmetric with the `solver` rule. Disagreement raises the plan's message plus the
    offending keys.
18. **`tests/test_spec_key_null_policy.py` gained one `_SENTINELS["policies"]` entry.**
    Its own guard requires a sentinel for every `UNSUPPLIED` + `CARRY_IF_ABSENT` key, and
    the plan makes `policies` exactly that. It is the same kind of self-forced edit as the
    allowed `test_assemble_spec_key_registry.py` one.
19. Under an authored `policies.dispatch`, compile re-mirrors the resolved params into
    every key already present in `solver_params` and both `planner.deployment.params`
    copies. With the literal rule, deleting an authored param would have left the old
    mirrored value driving accounting while the policy ran its default.
20. `resolve_policies_block` fills every implemented slot with its default, so `{}`
    means "all defaults".
21. The legacy translation is echoed into the recipe as `policies`, so a recompiled
    legacy spec's provenance becomes "authored". This is how "recipe policies = authored"
    was read.
22. Compile folds a profile's `use_spatial_matching` into `candidates` (legacy
    provenance) and removes the key; with an authored block a non-`true` value is a
    `SpecValidationError`. `apply_policy_overrides` removes it too when it writes a
    `policies` block onto an old bundle, otherwise the runtime resolver would refuse it.
23. Run name: `· <dispatch>` is appended only when the override names a dispatch type; a
    params-only override adds no label but stamps `source: "override"`.
24. `normalize_generate_spec` (legacy generation path) carries `policies` and translates
    `policies.dispatch` into the `solver`/`solverParams` spelling that path knows; the
    `SPEC_KEYS` legacy-literal guard caught the silent drop.
25. Frontend: no run launcher had a per-run solver dropdown (`SimulationTab` never sent
    `solver`), so nothing there needed rewiring. The run route turns a body `policies`
    object into `--policy`/`--policy-param` flags. Because the route spawns
    `run-simulation` in the background, a python-side rejection is not returned to the
    dashboard synchronously (already true for `solver`). Hydrated params are kept as
    authored; schema defaults are displayed but not written unless edited.

### P3
26. Battery robustness checks mean "returns a `Plan` without raising"; contract breaches
    are reported only under `structure.*`, so each broken policy fails exactly one check.
    `rejects_out_of_bounds` and `rejects_bad_choice` are one check each per policy (the
    detail lists every field), keeping `DISPATCH_CHECKS` a fixed tuple (21 checks).
27. The determinism fixtures keep fewer trucks than `per_order` (40): finding,
    `spatial.iter_candidate_pairs` truncates with `out[:cap]` in input order, so the
    framework's spatial candidate set is itself order-sensitive whenever the cap binds.
    That is framework behaviour and is left unchanged (byte identity).
28. Capture: file names add `_r<round>` for pooled scopes; the fixture also records
    `rng` state, `candidate_kind` and `horizon_steps` (what makes replay exact);
    `CaptureSettings` defaults `every_n_ticks=1`, `max_files=40`; the hook protocol is
    `before_decide(ctx)` then `capture(ctx, plan, problems)` before `check_plan`. Replay
    rebuilds the accounting cost from the policy's dual-cycle params
    (`framework_accounting_cost`), because the live `_solver_params` closure is not in the
    context. `AssignmentApp._dispatch_runner`'s cache key does not include
    `policy_capture` (it is set before agents spawn and never changes).
29. Perf budgets measured on this box (load ≈ 4.5/32 cores): GreedyNearest 108-133 ms →
    500 ms (floor); RandomAssignment 170-183 ms → 550 ms. Numbers are at the decorators.
30. CLI: `policy bench` prints the control plane's fixed-width table (a rich table at 80
    columns truncated every column); bad input exits 2, a battery FAIL exits 1; `solver
    list --json` now emits the dispatch slot catalog (no consumer of the old shape).

### Not done in this round (needs the user)
- Every live gate (§6 item 9): merge into `/home/user/openride_apps`, celery restart,
  500-truck n=3 runs, the browser check, a `--capture-dispatch` run.
- Out-of-repo docs: doc 26 (draft at `docs/plans/26-policy-slots.draft.md`), the
  CLAUDE.md §5 pointer, the doc 11 update, the `/restart-for` table entry.
- **The live dashboard's Scenario tab cannot save until the backend serving it exposes
  `known-policies.slots`** (the main tree does not yet); this is the intended
  no-fallback behaviour, but it is user-visible now because `next dev` serves this
  branch.
