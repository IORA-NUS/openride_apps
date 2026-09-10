"""Unit tests for scripts/perf_decompose.py.

Pure parsing/analysis logic, exercised against small synthetic sim.log fixtures
written to a tmp_path — no real run data, no services, no network. The script
itself is read-only and this test never touches apps/output/.
"""

import importlib.util
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "perf_decompose.py"

spec = importlib.util.spec_from_file_location("perf_decompose", SCRIPT_PATH)
perf_decompose = importlib.util.module_from_spec(spec)
sys.modules["perf_decompose"] = perf_decompose
spec.loader.exec_module(perf_decompose)


def _agent_line(ts, step):
    return f"{ts} INFO root agent_scheduler Step: {step}\n"


def _service_line(ts, step):
    return f"{ts} INFO root service_scheduler Step: {step}\n"


def _stat_line(ts, total_agents, stepping_agents, sleeping=0):
    d = {
        "completed": 0,
        "ready": 0,
        "error": 0,
        "shutdown": 0,
        "waiting": 0,
        "booting": 0,
        "sleeping": sleeping,
        "stepping_agents": stepping_agents,
        "total_agents": total_agents,
    }
    return f"{ts} INFO root self.agent_stat[self.time] = {d!r}\n"


def make_run(tmp_path, run_id, steps_and_stepping, wall_seconds=None, write_summary=True):
    """steps_and_stepping: list of (ts_str, stepping_agents) for consecutive steps."""
    run_dir = tmp_path / run_id
    run_dir.mkdir(parents=True)

    lines = []
    for i, (ts, stepping) in enumerate(steps_and_stepping):
        lines.append(_agent_line(ts, i))
        lines.append(_service_line(ts, i))
        # service scheduler's own (small) stat, interleaved as observed in real logs
        lines.append(_stat_line(ts, total_agents=3, stepping_agents=1))
        lines.append(_stat_line(ts, total_agents=1000, stepping_agents=stepping, sleeping=stepping))
    (run_dir / "sim.log").write_text("".join(lines))

    if write_summary:
        summary = {
            "run_id": run_id,
            "scenario": "unit_test_scenario",
            "status": "completed",
            "ok": True,
            "wall_seconds": wall_seconds if wall_seconds is not None else 0.0,
            "steps": len(steps_and_stepping),
            "total_steps": len(steps_and_stepping),
            "sim_days": 1.0,
            "fleet": {
                "num_orders_completed": 42,
                "empty_ratio": 0.1,
                "dual_cycle_rate": 0.5,
            },
        }
        (run_dir / "summary.json").write_text(json.dumps(summary))

    return run_dir


def test_pairs_only_consecutive_steps_and_buckets_correctly(tmp_path, monkeypatch):
    monkeypatch.setattr(perf_decompose, "OUTPUT_DIR", tmp_path)

    # 4 steps -> 3 pairs. Duration of pair (N, N+1) is attributed to step N's
    # stepping_agents, so step 3 (the last marker) never contributes a duration.
    ts = [
        "2026-01-01 00:00:00,000",  # step 0, stepping=0 (idle);  pair 0->1 = 100ms
        "2026-01-01 00:00:00,100",  # step 1, stepping=9 (light); pair 1->2 = 200ms
        "2026-01-01 00:00:00,300",  # step 2, stepping=500 (heavy); pair 2->3 = 1000ms
        "2026-01-01 00:00:01,300",  # step 3, stepping=1 (unused: no step 4 to pair with)
    ]
    stepping = [0, 9, 500, 1]
    make_run(tmp_path, "run_test_a", list(zip(ts, stepping)), wall_seconds=1.3)

    rd = perf_decompose.load_run("run_test_a")
    assert rd.log_error is None
    assert rd.summary_error is None
    assert len(rd.steps) == 4
    assert rd.unmatched_stat_steps == 0

    pairs = perf_decompose.paired_deltas(rd.steps)
    assert len(pairs) == 3

    deltas_by_stepping = {s.stepping_agents: round(delta, 3) for s, _n, delta in pairs}
    assert deltas_by_stepping[0] == 0.1
    assert deltas_by_stepping[9] == 0.2
    assert deltas_by_stepping[500] == 1.0


def test_non_consecutive_step_gap_is_excluded_from_pairing(tmp_path, monkeypatch):
    monkeypatch.setattr(perf_decompose, "OUTPUT_DIR", tmp_path)

    run_dir = tmp_path / "run_test_gap"
    run_dir.mkdir()
    lines = [
        _agent_line("2026-01-01 00:00:00,000", 0),
        _service_line("2026-01-01 00:00:00,000", 0),
        _stat_line("2026-01-01 00:00:00,000", 1000, 10),
        # step 1 missing entirely
        _agent_line("2026-01-01 00:00:05,000", 2),
        _service_line("2026-01-01 00:00:05,000", 2),
        _stat_line("2026-01-01 00:00:05,000", 1000, 20),
    ]
    (run_dir / "sim.log").write_text("".join(lines))

    rd = perf_decompose.load_run("run_test_gap")
    assert rd.log_error is None
    assert len(rd.steps) == 2  # steps 0 and 2 parsed
    pairs = perf_decompose.paired_deltas(rd.steps)
    assert pairs == []  # 0 -> 2 is not a consecutive pair, so it must be dropped


def test_missing_summary_json_reported_not_raised(tmp_path, monkeypatch):
    monkeypatch.setattr(perf_decompose, "OUTPUT_DIR", tmp_path)
    run_dir = tmp_path / "run_no_summary"
    run_dir.mkdir()
    (run_dir / "sim.log").write_text(_agent_line("2026-01-01 00:00:00,000", 0))

    rd = perf_decompose.load_run("run_no_summary")
    assert rd.summary is None
    assert rd.summary_error is not None
    assert "summary.json" in rd.summary_error


def test_missing_run_directory_reported_not_raised(tmp_path, monkeypatch):
    monkeypatch.setattr(perf_decompose, "OUTPUT_DIR", tmp_path)
    rd = perf_decompose.load_run("run_does_not_exist")
    assert rd.summary_error is not None
    assert rd.log_error is not None


def test_missing_sim_log_reported_not_raised(tmp_path, monkeypatch):
    monkeypatch.setattr(perf_decompose, "OUTPUT_DIR", tmp_path)
    run_dir = tmp_path / "run_no_log"
    run_dir.mkdir()
    (run_dir / "summary.json").write_text(json.dumps({"wall_seconds": 1.0}))

    rd = perf_decompose.load_run("run_no_log")
    assert rd.summary_error is None
    assert rd.log_error is not None
    assert rd.steps == []


def test_service_scheduler_small_total_agents_never_used(tmp_path, monkeypatch):
    """The service_scheduler's own stat (total_agents=3 in real logs) must never be
    mistaken for the fleet-wide agent_scheduler stat, even though both dicts share
    an identical shape and can appear in either order between two step markers."""
    monkeypatch.setattr(perf_decompose, "OUTPUT_DIR", tmp_path)
    run_dir = tmp_path / "run_order_flip"
    run_dir.mkdir()
    lines = [
        _agent_line("2026-01-01 00:00:00,000", 0),
        _service_line("2026-01-01 00:00:00,000", 0),
        # small stat FIRST this time (order flips between steps in real logs)
        _stat_line("2026-01-01 00:00:00,050", 3, 3),
        _stat_line("2026-01-01 00:00:00,060", 1000, 7),
        _agent_line("2026-01-01 00:00:00,500", 1),
        _service_line("2026-01-01 00:00:00,500", 1),
        _stat_line("2026-01-01 00:00:00,550", 1000, 0),
        _stat_line("2026-01-01 00:00:00,560", 3, 1),
    ]
    (run_dir / "sim.log").write_text("".join(lines))

    rd = perf_decompose.load_run("run_order_flip")
    assert rd.steps[0].stepping_agents == 7
    assert rd.steps[0].total_agents == 1000
    assert rd.steps[1].stepping_agents == 0
    assert rd.steps[1].total_agents == 1000


def test_pctl_matches_known_values():
    assert perf_decompose.pctl([1.0, 2.0, 3.0, 4.0, 5.0], 0.5) == 3.0
    assert perf_decompose.pctl([5.0], 0.9) == 5.0
    assert perf_decompose.pctl([], 0.5) != perf_decompose.pctl([], 0.5)  # nan != nan
