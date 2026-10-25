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
| `base_solver.py` | `1c941b56b30c65ef5cc74d736fd2ea90cf066f72395deef6ea8ea28f3fc3b812` | verbatim |
| `edge_builder.py` | `d2175e1313ccafb42b1a957a78687cd5e6999525eb93293f721ec6fe0f72ab52` | verbatim |
| `network_flow_solver.py` | `799ffaf24d6d597663a490c101b8a2020db514272ec3bd6926ecd51f4729e99f` | verbatim |
| `fairness_solver.py` | `0742c5bbcfcbd0f685c41e01e1053c8fc2ca9da500b189b1faf0bd6f60b6eb27` | verbatim |
| `lib/__init__.py` | `ced2e5ad7d2aa163a0c372142d2a09e009d5113afa80263ae914c8ac6e0330df` | verbatim |
| `lib/api.py` | `92db3b3d6eb437bf12f13b0625ec7db2720b90ecde87c0222d5a45bade56776d` | verbatim |
| `lib/schema.py` | `05d04557e4c5f0f9372beae00cf8397d84b239c62fd0545b45ad7ff7bbebc1a6` | verbatim |

Not vendored: upstream `README.md` (the contract doc; read it at the upstream path).

## Local modifications

None yet (this commit is the pristine copy the golden was captured from).

## Golden

`tests/data/coop_dispatch_upstream_golden.json` was captured from the PRISTINE upstream
files (hashes above) by `tests/data/capture_coop_dispatch_upstream_golden.py` before any file was edited, with OR-Tools 9.15.6755 (SCIP backend; gurobipy not installed).
