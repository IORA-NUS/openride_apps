# coop_dispatch: vendored cooperative-dispatch solver

Upstream: `/home/user/solvers/dispatch_solvers/standalone/` (the user's standalone
network-flow / fairness solver; not a git repository; file mtimes 2026-10-19 02:09).
Vendored: 2026-10-25, workstream E of the NetworkFlow day-plan build (plan N2).

This package imports `ortools` at module top (as upstream does). Nothing under
`policies/framework`, `policies/dispatch` (including `dispatch/library/`) or any other
framework module may import it; only a solver's child process does.

## Upstream sha256 (the files as copied)

The table is machine-read by `tests/test_coop_dispatch_vendor.py` and
`tests/data/capture_coop_dispatch_upstream_golden.py`. It lists the UPSTREAM hash of every
vendored file. Files marked *verbatim* must still match it byte for byte here.

| `file` | `sha256 (upstream)` | status |
|---|---|---|
| `__init__.py` | `22a98c64249d9b90f1297e61d96ad6490f489d9debaaac8fe210a3e531520831` | verbatim |
| `base_solver.py` | `1c941b56b30c65ef5cc74d736fd2ea90cf066f72395deef6ea8ea28f3fc3b812` | modified |
| `edge_builder.py` | `d2175e1313ccafb42b1a957a78687cd5e6999525eb93293f721ec6fe0f72ab52` | verbatim |
| `network_flow_solver.py` | `799ffaf24d6d597663a490c101b8a2020db514272ec3bd6926ecd51f4729e99f` | modified |
| `fairness_solver.py` | `0742c5bbcfcbd0f685c41e01e1053c8fc2ca9da500b189b1faf0bd6f60b6eb27` | verbatim |
| `lib/__init__.py` | `ced2e5ad7d2aa163a0c372142d2a09e009d5113afa80263ae914c8ac6e0330df` | verbatim |
| `lib/api.py` | `92db3b3d6eb437bf12f13b0625ec7db2720b90ecde87c0222d5a45bade56776d` | verbatim |
| `lib/schema.py` | `05d04557e4c5f0f9372beae00cf8397d84b239c62fd0545b45ad7ff7bbebc1a6` | verbatim |

Unchanged upstream caveat the caller must handle: `use_trailer_cost` defaults to True and
then charges 30,000 m per depot start arc (plan D12(d)); pass `use_trailer_cost=False`.

Not vendored: upstream `README.md` (the contract doc; read it at the upstream path).

## Local modifications (all options default OFF)

`network_flow_solver.py` (`NetworkFlowSolver.solve`), new optional `params` keys:

- `start_nodes: {sid: {"company": c, "capacity": k}}` with `start_edges: [(sid, order)]`
  and `start_edge_cost: {(sid, order): meters}`. Each start node is a source of capacity
  k (int >= 1, default 1; out <= k) inside its company's commodity: k identical trucks
  (same class, exact position and ready time) merged into one node. `capacity` absent and
  `capacity: 1` build the identical model. A company that has start nodes gets no depot
  start arcs and its `vehicle_limit` is not applied.
- `defer_penalty: float`: an order may stay unplanned (`sum_c a[i,c] + u_i == 1`), at
  objective cost `P * u_i`. Result gains `unplanned`.
- `eligible: set[(order, company)]`: only these pairs get `a` / `x` variables
  (working hours per company or truck class).
- `time_limit_s`, `mip_gap`: passed to `create_solver_with_fallback`; `accept_feasible`:
  a FEASIBLE incumbent is extracted like OPTIMAL. With any option given, stats gain
  `status_name`, `num_constraints_model` (the solver's real count), `num_start_nodes`,
  `num_start_arcs`, `time_limit_s`, `mip_gap` and (with deferral) `num_unplanned`.
  A FEASIBLE status means a limit stopped the search: the plan is then wall-clock
  dependent, and the caller must stamp it non-deterministic (plan D12(c)).
- Edge index (ALWAYS on): in/out adjacency built once in edge order, replacing the
  per-(order, company) scans over every edge. Same constraints, same terms, same order,
  same variables: the exported LP model is byte-identical to upstream's (pinned by
  `tests/test_coop_dispatch_vendor.py` against `tests/data/coop_dispatch_upstream_golden.json`).

New keys appear in the result / stats only when an option is given, so options-off output
is identical to upstream's.

`base_solver.py`: `create_solver_with_fallback(preferred_backend, time_limit_s=None,
mip_gap=None)`; `None` keeps the upstream environment behaviour (`SCIP_TIME_LIMIT_SECONDS`,
default 5400 s). An explicit time limit is floored at 1 ms (OR-Tools reads 0 as "no
limit"). The Gurobi adapter applies `TimeLimit` / `MIPGap`. `solve_model(solver)` (called
by `NetworkFlowSolver` instead of `solver.Solve()`) applies a stored `mip_gap` to pywraplp
backends through `MPSolverParameters`; without one it is exactly `solver.Solve()`.
`fairness_solver.py` is verbatim and does NOT get these options.

## New modules (not upstream)

- `time_window_edges.py`: time-window edge builder, linear in jobs x window, with K caps.
  Beyond the plan's window rule it requires a successor to END after its predecessor
  (ties by input order), which makes every arc set a DAG; the MIP has no subtour
  elimination, so this is what keeps it from returning truck-less job cycles.
- `chains.py`: per-start-node chains from a solved `routes` map.
- `min_cost_flow.py`: exact single-commodity min-cost-flow backend for cooperation
  components where `mcf_applicable` holds (complete directed adjacency incl. self pairs,
  and all classes share `class_attrs`: size / restricted areas / working hours). Costs
  are rounded to integer meters; `objective_float` re-prices the plan at the original costs.
- `backends.py`: `solve_by_component(params, backend="auto"|"mincostflow"|"mip")` (flow
  where exact, MIP elsewhere, backend recorded per component), `served_upper_bound`,
  `served_reference` and `quality_guard` (a limit-stopped MIP incumbent serving < 95% of
  the reference is `poor_incumbent`; addendum B2).
- `limits.py`: `memory_cap_preexec` (child `RLIMIT_AS`, default 4 GiB) and
  `check_mip_arc_budget` (default 500,000 `x` variables; addendum minor 8).

## Compact arc form (follow-up E2)

- `time_window_edges.build_time_window_arrays` returns `TimeWindowArrays` (numpy index
  arrays, the dict API's exact arcs in its order); `build_time_window_edges` is now its
  dict view.
- `min_cost_flow.solve_min_cost_flow` also takes `edges_array` / `start_edges_array`
  (`{"src", "dst", "cost"}` index arrays into `orders` / `list(start_nodes)`) and builds
  the network vectorised; `backends.component_params` slices them; `limits.arrays_to_lists`
  turns them into the MIP's lists (the MIP path, the served bound and the arc budget use it).
- `chains.extract_chains(..., job_order=)`: `chains_by_start` lists up to `capacity` chains
  per start node, ordered by the first job's position in `job_order` (else `str(first)`);
  `by_start` keeps the capacity-1 view.

## OR-Tools version

Tested with **ortools 9.14.6206**: the newest release whose METADATA accepts this repo's
pins (`numpy>=1.13.3`, `pandas>=2.0.0`, `protobuf<6.32,>=6.31.1`). 9.15 requires
`numpy>=2.0.2` and cannot install beside `numpy==1.26.3` (addendum M3). A clean py3.11
`pip install --dry-run -r requirements.txt ortools==9.14.6206` resolves (protobuf 6.31.1,
absl-py, immutabledict, typing_extensions added). The golden is byte-identical under 9.14
and 9.15.

## Golden

`tests/data/coop_dispatch_upstream_golden.json` is captured from the PRISTINE upstream
files by `tests/data/capture_coop_dispatch_upstream_golden.py`, which refuses to run unless
every upstream file matches the hashes above and imports the solver from the upstream path
(never from this package). Instances 1-7 were captured before either modified file was
edited (commit 4e1ed6d); instance 8 (duplicate `edges_input` entries) was added afterwards,
again from the pristine upstream, and instances 1-7 came out byte-identical. OR-Tools
9.15.6755 at capture, re-captured byte-identical under 9.14.6206; SCIP backend (gurobipy is not installed, so the Gurobi adapter is untested
against a real Gurobi; a stub test checks it receives `TimeLimit` / `MIPGap`).
