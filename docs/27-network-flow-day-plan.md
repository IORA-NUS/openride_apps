# 27. NetworkFlowDay: a day plan per truck, dispatched head by head

> **Status (2026-10-25): built on branch `nf/integration` @ c943e10 (cut from
> `feat/policy-slots` @ 88ea74b). NOT merged into `feat/policy-slots`, NOT in the main tree
> `/home/user/openride_apps`, never run live.** The adversarial review of c943e10
> (`REVIEW_NF.md`, "Verification of c943e10") says it is ready for live gates on
> `consortium_collab_7d_wh` launched with `--assignment-cadence 6`. One MAJOR remains open
> (V-M1, differing per-haulier hours on a cooperative arm, §8). It is being fixed in the FX3
> round.
>
> This repo has no `docs/reference/features/` directory, so the file sits under `docs/`.
> Its number continues the out-of-repo index (`/home/user/docs/reference/features/`, 01-26).
> The generic framework parts (planning ticks, prefetch, `top_only`, the step hold, the
> cadence / orders-per-tick / working-hours inputs, refusals R2-R9) are documented in
> `docs/policy_authoring.md` §14. This file covers the solver.

## 1. What it does

`NetworkFlowDay` (`apps/container_logistics/policies/dispatch/library/network_flow_day.py`,
`@policy(slot="planner", version="1", badge="central", requires=("ortools",), top_only=True)`)
is a planner-slot top that plans every truck's whole day and then hands each free truck the
next job of its plan.

- **Planning ticks** (day start, then every `nf_replan_every_steps` steps; declared in `bind`
  as `planning = {at_day_start: True, every_steps: nf_replan_every_steps, foresight: "day",
  max_hold_seconds: nf_max_hold_s}`). The sim holds the step. The wrapper builds one instance
  from every online truck, the busy trucks' open hauls and ALL of the run day's
  `created` / `unassigned` orders, released or not (ORACLE foresight). It solves it in a
  child process and stores one itinerary per truck as plan version `d{day}v{k}`. Locked jobs
  (`dispatched` / `done`) never move; a re-plan rewrites only the open tail, across every
  still-working truck. An instance whose hash is unchanged is not re-solved
  (`skipped: unchanged`). A planning tick commits nothing and prefetches, per truck, its
  oldest unresolved dispatched job and its head (at most 2 ids per truck, capped at 4000).
- **Dispatch ticks** (every assignment cadence step). For each free truck, in id order, the
  wrapper walks its itinerary with the head rules below, using `tick.lookup` (the prefetched
  order documents, each with `claimed_by`):

  | Order state of the head | Rule |
  |---|---|
  | assigned / pickup_in_progress / in_transit / dropoff_in_progress / completed | ours: `dispatched` (`done` if completed), next; another truck's: `failed:taken`, next |
  | cancelled | `failed:cancelled`, next |
  | created | not released yet: the truck WAITS |
  | unassigned, claimed by another truck | `failed:taken`, next |
  | unassigned, claimed by our truck | `dispatched` (lifecycle lag), next |
  | unassigned, free, in the tick | `permitted`: PAIR; else `failed:infeasible`, next |
  | unassigned, free, NOT in the tick | the two haul reads disagree: WAIT; a 2nd consecutive tick of it gives `failed:taken` |
  | not looked up | in the tick: as "free, in the tick"; else WAIT |

  A returned pair is held as tentative and written `dispatched` on the next tick, once the
  framework is seen to have committed that result (a non-empty lookup proves it). A discarded
  result's pairs go back to `planned` without spending a retry (noted `uncommitted`). A job
  dispatched to a truck that is free again while the order is still unassigned and unclaimed
  was refused: it is paired once more (`_MAX_RETRIES` = 1), then `failed:refused`.
  **An empty itinerary means the truck sleeps until the next plan.** It is never refilled by an
  online matcher.

The framework still checks every pair (`permitted`, `check_result`), commits it, does the
share accounting and drives the run stats. The solver reads `cooperation`, `working_hours`,
`unassigned_order_max_wait_steps` and `assignment_cadence_steps` from the world, all at bind.

## 2. Params (`NetworkFlowDay.Params`)

| Param | Default | Range | Meaning |
|---|---|---|---|
| `nf_backend` | `"auto"` | `auto`, `mincostflow`, `mip`, `greedy` | solve backend (§4). Validated in `__post_init__`, deliberately NOT a `choices` param, so the battery runs no arm per backend |
| `nf_time_limit_s` | 20.0 | (0, 300], and at most `nf_max_hold_s - 5` | time limit of each MIP component |
| `nf_max_hold_s` | 30 | 6..300 | declared `max_hold_seconds`. The solve child is killed 5 s before it (`_HOLD_MARGIN_S`) |
| `nf_replan_every_steps` | 18 | >= 1 or unset | re-plan interval (18 steps = 72 min at 240 s/step); unset = day start only |
| `nf_replan_idle_trucks` | `True` | bool | a re-plan may give new jobs to trucks whose itinerary ran out |
| `nf_cadence_steps` | unset | 1..360 | override of the dispatch cadence the latency model assumes. Unset = the run's own (world `assignment_cadence_steps`). Set and different from the run's: REFUSED at bind. Offline default 6 |
| `nf_memory_cap_gib` | 4.0 | (0, 64] | `RLIMIT_AS` of the solve child |
| `nf_wait_limit_min` | 240.0 | (0, 1440] | longest wait between two chained jobs (N0: 240 beats 120 by -2.9 % empty km) |
| `nf_late_slack_min` | unset | 0..1440 | lateness a chain link may absorb; unset = per pickup/dropoff type pair from the time model |
| `nf_defer_penalty_km` | 200.0 | (0, 10000] | cost of leaving a job unplanned (above any empty leg) |
| `nf_stability_bonus_m` | 50.0 | 0..100000 | discount on the previous version's arcs |
| `nf_propagate_chains` | `False` | bool | re-plans push each old chain by its truck's excess lateness (§7) |
| `nf_propagate_max_push_min` | unset | 0..1440 | with propagation: a chain pushed further is re-planned freely |
| `nf_mip_gap` | 0.01 | 0..1 or unset | relative MIP gap |
| `nf_mip_k_succ` | 10 | >= 1 or unset | successor / start arcs kept per job on the MIP path only |
| `nf_mip_arc_budget` | 500000 | >= 1 | largest MIP attempted; above it the component goes greedy |
| `nf_guard_threshold` | 0.95 | 0..1 | a MIP / greedy plan must serve this share of the exact bound, else the previous version is kept |

Fixed internals (`solver_lib/day_plan/instance.py::DayPlanParams`): no K cap on the
min-cost-flow path (N0: K=20 cost +12.6 % empty km), planning km = haversine x 1.3, positions
hashed at 4 decimals (~11 m). The overdue-sweep margin is one dispatch cadence (M11): a job
whose `(request_step + UNASSIGNED_ORDER_MAX_WAIT_STEPS) * step_seconds` falls before its
planned dispatch plus one cadence is excluded from the solve.

Provenance stamped at `run_config.meta.policies.planner.provenance.planner`: `badge`,
`backend`, `time_model` (digest), `cadence_steps`, `cadence_source` (`world` / `param` /
`default`), `replan_idle_trucks`.

## 3. The instance and the time model

`solver_lib/day_plan/instance.py::build_inputs` (plain data in, a hashable `PlanInputs` out):

- **Classes** `(haulier, truck_size, restricted_areas)` are the network commodities. A job
  owner without a truck in the instance gets a synthetic vehicle-less class.
- **Time axis**: seconds since `REFERENCE_TIME` (elapsed). Working hours give each class its
  windows; an overnight shift gives two windows in one plan (the second start is
  `{truck}@w{k}`).
- **Truck start**: `ready = max(now, first window start, predicted end of its locked job)`,
  at that job's dropoff, else at the truck's anchor. Engaged trucks use the haul's
  `stats.reposition_started_at` when the wrapper lost the pair time. Identical trucks (class,
  EXACT position, ready time) are merged into one start node with capacity (N0: exact;
  rounding the key broke reachability).
- **Jobs**: release `r = max(request_s, now_s)`, and with hours `max(r, shift start)`, so an
  order released before the shift waits for it (M4). End
  `e = r + latency(cadence) + job_seconds(features)`.

The time model (`solver_lib/day_plan/time_model.py` + the generated
`time_model_data.py`, written by `scripts/fit_day_plan_time_model.py`) predicts
dispatch-to-completion seconds as a per-cell median over (pickup/dropoff location type,
4-hour elapsed bucket of the dispatch moment, km bucket, queueing facility), with backoff.
Fitted on `run_20261021_175341` (`consortium_collab_7d`, two-pairs, GreedyNearest,
partitioned, paho-shared, 14,493 trips with 100 % OSRM geometry). Leave-one-day-out hold-out:
**MAE 8,948 s (~2.5 h)**, P90 |residual| 23,040 s, P90 signed (late) 13,440 s. Late slack per
type pair (`LATE_SLACK_MIN_BY_PAIR`): CT|CU 140, CT|MT 260, CU|CT 105, CU|MT 280, MT|CT 280,
MT|CU 290 min.

## 4. Backend selection

Per cooperation component of the class graph (`solver_lib/day_plan/child.py::solve_inputs`):

- **`mincostflow`** where `coop_dispatch/min_cost_flow.py::mcf_applicable(component,
  cooperation, class_attrs)` holds: complete directed adjacency incl. self pairs, AND every
  class sharing `[size, areas, shift]`. Exact, integer-metre costs, no K cap. All 17 shipped
  bundles are uniform (20ft, same areas), and the three shipped structures (`no-coop`,
  `two-pairs`, `full`) are cliques, so under uniform hours every component takes this path.
- **`mip`** elsewhere (or everywhere under `nf_backend="mip"`): the vendored library MIP
  (`coop_dispatch/`, OR-Tools pywraplp, SCIP) on K-capped arcs, refused above
  `nf_mip_arc_budget`, FEASIBLE accepted, checked by the quality guard against the exact
  served bound (`coop_dispatch/backends.py::served_reference` / `quality_guard`).
- **`greedy`** when the MIP has no solution, is over budget, is a poor incumbent, or its share
  of the deadline is under 1 s (`fallback: deadline`); and for the whole solve under
  `nf_backend="greedy"` (no OR-Tools needed). A greedy fallback below the guard refuses the
  solve (`poor_incumbent`), so the previous version stays.

One deadline per planning tick (M2): `nf_max_hold_s` from the tick's start minus 5 s. The
child splits what is left across its MIP components and keeps a greedy reserve. A day with NO
plan (none yet, or only an earlier day's) never stays empty: the guard is waived
(`accepted_poor`) and a failed solve falls back to a full greedy plan (`fallback: greedy`).
The child runs under the agent's own interpreter (`sys.executable`), in its own session,
under `RLIMIT_AS`, killed at the deadline (`runner.py`; errors `SolveTimeout`,
`SolveCrashed(crashed | out_of_memory)`, `SolveRejected(poor_incumbent | invalid_output)`).
The backend used per component is in the plan note (`backends`) and in `meta.day_plan.backends`.

If OR-Tools is not importable, the catalog lists the solver unavailable and a run selecting it
is refused (`requires`). Offline (battery, tests) the wrapper falls back to `greedy` with a
WARNING once per run.

## 5. Redis record (`solver_lib/day_plan/store.py`)

`RedisPlanStore` for a live run (`tick.run_id` set), a private in-memory store offline. URL
`OPENRIDE_PLAN_REDIS_URL`, default `redis://127.0.0.1:6379/2`. **db 0 is refused** (the Celery
broker). Container `redis` (`redis:7.4-alpine`, published on 6379); redis-py 8.1.0 is in the
main venv. Client socket / connect timeouts 2 s.

```
plan:{run}:current              STRING  "d3v14"           (SET last in every MULTI)
plan:{run}:d3v14                STRING  zlib(JSON) blob of the whole version (all kept)
plan:{run}:d3v14:truck:{t}      LIST    [o7, o2, o9]      (current version only)
plan:{run}:d3v14:job:{o}        HASH    truck, seq, status, reason, <job fields> (current only)
plan:{run}:status               HASH    job -> JSON {status, reason, truck, version}
plan:{run}:kpi:d3               HASH    field -> JSON number / string
plan:{run}:versions             HASH    d3 -> 14
```

- **TTL 14 days** (`TTL_SECONDS`) on every key, set in the same MULTI. Budget 50,000 keys /
  256 MB per run (`OPENRIDE_PLAN_MAX_KEYS` / `OPENRIDE_PLAN_MAX_BYTES`). Review measurement:
  ~55 KB per version blob, ~10 MB per 7-day run. Ids must match `[A-Za-z0-9_.:@+-]{1,200}`
  (real ids are 24-hex).
- Statuses: `planned`, `dispatched`, `done`, `failed` (+ reason), `unplanned`, `undone`.
  Across a day change only `dispatched` jobs carry over; yesterday's still-open jobs roll
  into the new day's instance if they are still open orders.
- Failures never fail a tick. `PlanStoreUnavailable` keeps the change in memory and resyncs
  the whole state on the next write; a refused write keeps the previous version. Both are
  noted `store_failed`. After an agent restart the first tick with a run id reloads the
  record (`load_current`) and folds a `rebuild` note into the run stats.
- Single writer per run; another writer is detected (`PlanStoreConflict`), not arbitrated.

## 6. KPIs: `run_config.meta.day_plan` (`solver_lib/day_plan/stats.py::DayPlanStats`)

The top's run-stats aggregator (`meta_key = "day_plan"`). Pushed on a new day, a failed
re-plan, a fallback plan, a rebuild, else every 10 folded ticks, and at agent close.

- Top level: `schema` (1), `ticks`, `plan_ticks`, `restarts`, `deterministic`, `backends`
  `{backend: components}`, `solve_ms {n, p50, max}`, `build_ms {p50, max}`,
  `wall_ms {p50, max, sum}`.
- `totals` and `days.d{n}` (and `days.d{n}.by_haulier.{h}`): `plan_jobs`, `plan_jobs_done`,
  `plan_jobs_dispatched`, `plan_jobs_failed {reason: n}` (`taken`, `cancelled`, `infeasible`,
  `refused`), `plan_jobs_undone`, `plan_jobs_moved`, `plan_jobs_moved_by {truck, position,
  to_unplanned}`, `versions`, `replans_skipped`, `replans_failed {reason: n}`. Per day also
  `plan_first_leg_km {haulier: km}` (from the day's v1), `prev_arcs` (arc survival on
  re-plans), `fallback_plans`, `planned_last`, `unplanned_last`, `open_at_day_end`.
- **Attribution (M11):** a job belongs to the day of the version that FIRST planned it; its
  status is the latest one.
- **`incidents`** (the CLAUDE.md §6.21 class: a degraded run must not read `ok`):
  `store_failed`, `backend_fallback {backend: n}`, `late_plans`, `late_steps_max`,
  `no_plan_ticks`, `plan_fallbacks {reason: n}`, `fallback_failed`,
  `component_fallbacks {reason: n}`, `accepted_poor`, `uncommitted_pairs`. **Check
  `incidents` before quoting any NetworkFlowDay run.** A dispatch tick with no plan also logs
  an ERROR once per day (celery journal).

The plan KPIs are the plan's own bookkeeping. The research metric is still the sim's
(`empty_ratio`, `dual_cycle_rate`, served orders). **Do not gate on `plan_jobs_moved`** (§7).

## 7. N0 and build measurements (offline, not the sim KPI)

N0 spike (`scratchpad/nf/n0/N0_REPORT.md`, `consortium_collab_7d`, placeholder time model,
fixed times, no queues). Verdict GO, provided the company-indexed MIP is not used:

| | day 1 | day 2 |
|---|---|---|
| orders | 4,980 | 4,994 |
| greedy (oracle-timed) empty km | 2,524 | 4,587 |
| exact min-cost flow empty km | 2,160 (-14.4 %) | 3,304 (-28.0 %) |
| build + solve | 2.1 + 1.1 s | 1.8 + 1.0 s |
| no start nodes (library 0-km depot), matched afterwards | +91 % | +65 % |

- The library's company-indexed MIP (SCIP / CBC) produced no usable plan in 60 s or 600 s at
  any K. The collapsed LP was integral (0 fractional variables), so min-cost flow is exact.
- CBC ignores its time limit and reports `INFEASIBLE` on always-feasible models.
- A 50 m stability bonus cut re-plan moves from 1,975 to 73 in N0 (zero drift).

Real-scale day 1 after the start-node merge (WS-H2): full arm, no hours 6.4 s / 1.06 GB under
the 4 GiB cap. With 06-20 hours: two-pairs 4.6 s / 357 MB, full 15.7 s / 2 GB (WS-H, before H2).

## 8. Known limitations

- **Distance-only objective, hence re-plan churn (M3).** Job times are fixed (release +
  latency + predicted duration) and lateness absorbed by late slack is never charged. The
  flow plans optimistic chains, and at the next re-plan most old arcs are infeasible. FX2 e2e
  (full arm, 06-20 hours): 300-500 jobs moved per re-plan; total default 1,907 done / 3,682
  moved. Moves only touch still-`planned` tail jobs, never a truck's current work.
- **Chain propagation is opt-in.** `nf_propagate_chains=True` gave 1,783 done / 2,193 moved
  (-6.5 % done for ~-45 % moves); caps of 10 / 30 / 90 min gave 1,898 / 1,839 / 1,789 done.
  A lateness cost would be a model change and a new arm; to be compared at n >= 3 after the
  gates.
- **V-M1: differing working hours inside a cooperative component.** The component is not
  `mcf_applicable`, so it takes the MIP, gets ~12 s, finds nothing and falls back to greedy
  with `accepted_poor` on every re-plan, while the run is still named NetworkFlowDay. The
  measured full arm: 1,577 served of a 2,144 bound (0.736). Two arms would then differ by
  solver quality, not cooperation. Not hit by the gate scenario (uniform hours). **Being
  fixed (FX3).** Until then, run no per-haulier-hours experiment.
- **The time model was fit on a congested baseline** (a GreedyNearest run whose depot gates
  queue a median 6.8-20 h each; WS-H found a median predicted job of 10.6 h). Its hold-out MAE
  is ~2.5 h. Under 06-20 hours only ~41 % of day-1 orders were plannable (WS-H). A plan's
  chains can only be as good as these durations.
- **Working hours 06:00-20:00 are a placeholder** on the ELAPSED axis (14:00-04:00 on the sim
  clock with `REFERENCE_TIME` 08:00). They are used only in the scratch scenario
  `consortium_collab_7d_wh` (`scratchpad/nf/scenarios/`); no shipped spec carries hours.
- Overnight second window: the `T@w1` start uses the truck's current anchor, not the end of
  its first-window chain (V-m3).
- The solve deadline is measured from `_plan_tick` entry, not from the hold start, so a slow
  Mongo fetch eats into the scheduler's grace (V-m2). Fallback re-plans hold ~20 s each
  (V-m6).
- A default `--policy planner=NetworkFlowDay` launch honestly runs at cadence 28 (stamped
  `cadence_steps: 28, cadence_source: world`). The design intends 6, and nothing warns
  (V-m4): **always pass `--assignment-cadence 6`**.
- The prefetch is stored before accounting / publish (V-m1 minor), and prefetch is capped at
  4000 ids (> 2000 trucks starve the late ids).

## 9. Live-gate procedure (not yet run)

Prerequisites, all OUTSIDE this branch's scope and each needing the user's go-ahead:

1. **Merge** `nf/integration` (with FX3) into `feat/policy-slots`, and that into the tree the
   celery workers and the sim main actually run from (today `/home/user/openride_apps`, which
   does not carry `feat/policy-slots` either).
2. **Install `ortools==9.14.6206` into the main venv** (`/home/user/openride_apps/venv`;
   pinned in `requirements.txt`, the newest release compatible with numpy 1.26.3 / pandas
   2.1.4; not installed there today). Without it the run is refused (`requires`).
3. **`systemctl --user restart openride-celery`**: new solver file and agent code
   (`assignment/agent.py`, `assignment/app.py`). The scheduler and sim main changes are
   picked up per run.
4. **Redis db 2** reachable from the workers (`OPENRIDE_PLAN_REDIS_URL`, default
   `redis://127.0.0.1:6379/2`).
5. **Scenario**: `consortium_collab_7d_wh` (`consortium_collab_7d` + 06-20 elapsed hours for
   all five hauliers). Copy its `spec.json` into the live `scenarios/` tree (or point
   `ORSIM_SCENARIOS_DIR` at the scratch folder for both compile and run) and **recompile**
   under the merged code (`openride scenario compile <slug>`): the scratch bundle was compiled
   before the WS-D commit. For a no-hours control, use `consortium_collab_7d` itself.
6. Launch, `--no-headless` only if the map is to be checked, detached (§8 of CLAUDE.md):

   ```
   openride run consortium_collab_7d_wh --policy planner=NetworkFlowDay \
       --assignment-cadence 6 --structure <arm>
   ```

   Expect run-name segments ` · <arm> · oracle:day · cadence:6`.
7. Check: `simulation_settings.PLANNING` (oracle true, axis elapsed, axis_origin),
   `ASSIGNMENT_CADENCE {steps: 6, source: override}`, `WORKING_HOURS`, raised `ORPHAN_TIMEOUT`
   (120 at a 30 s hold is below 180, so none); provenance `cadence_steps: 6`; `meta.day_plan`
   `incidents` all zero and `backends` only `mincostflow`; the perf summary's
   `held_steps` / `sum_step_ms_excl_hold`; `PLANNING HOLD EXPIRED` and `Auto Shutdown Agent`
   counts in `journalctl --user -u openride-celery` (0); geometry 100 % `osrm` (CLAUDE.md
   §6.21); `df -h /` (§6.23).
8. Compare arms only within one batch, n >= 3, against the same scenario with the same hours.
   An oracle day-plan arm is not comparable with an online arm as the same information
   setting, and neither run is comparable with the shipped `consortium_collab_7d` (hours are
   world physics).

## 10. Files

- Solver: `apps/container_logistics/policies/dispatch/library/network_flow_day.py`
- Day-plan helpers: `apps/container_logistics/policies/solver_lib/day_plan/`
  (`instance.py`, `child.py`, `runner.py`, `store.py`, `stats.py`, `time_model.py`,
  `time_model_data.py`)
- Vendored solver library: `apps/container_logistics/policies/solver_lib/coop_dispatch/`
  (`VENDOR.md`; `min_cost_flow.py`, `backends.py`, `limits.py`, `time_window_edges.py`, ...)
- Fit script: `scripts/fit_day_plan_time_model.py`
- Framework side: `docs/policy_authoring.md` §14
- Tests: `tests/test_network_flow_day.py`, `tests/test_day_plan_*.py`,
  `tests/test_coop_dispatch_*.py`, `tests/test_time_window_edges.py`; the ones that need
  OR-Tools skip without it (the main venv suite stays green with the solver unavailable).
