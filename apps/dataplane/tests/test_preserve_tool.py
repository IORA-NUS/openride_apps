"""Tests for apps.dataplane.tools.preserve_surviving_runs.

These never touch the real ``~/.openride/kpi-duckdb/`` files: every test builds a
throwaway per-run DuckDB under ``tmp_path`` with the same table shape.
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import time
from datetime import datetime, timedelta

import duckdb
import pytest

from apps.dataplane.tools import preserve_surviving_runs as tool

VENV_PYTHON = str(Path(__file__).resolve().parents[3] / "venv" / "bin" / "python")

SCHEMA = """
CREATE TABLE IF NOT EXISTS kpi_events (
    run_id VARCHAR NOT NULL,
    metric VARCHAR NOT NULL,
    value DOUBLE NOT NULL,
    sim_clock TIMESTAMP NOT NULL,
    ingested_at TIMESTAMP DEFAULT current_timestamp,
    PRIMARY KEY (run_id, metric, sim_clock)
);

CREATE TABLE IF NOT EXISTS kpi_breakdown_rows (
    run_id VARCHAR NOT NULL,
    scope VARCHAR NOT NULL,
    sim_clock TIMESTAMP NOT NULL,
    final BOOLEAN DEFAULT FALSE,
    entity_id VARCHAR NOT NULL,
    haulier_id VARCHAR,
    haulier_name VARCHAR,
    num_orders_completed BIGINT,
    empty_km DOUBLE,
    loaded_km DOUBLE,
    total_km DOUBLE,
    empty_ratio DOUBLE,
    active_hours DOUBLE,
    orders_per_day DOUBLE,
    dual_cycle_count BIGINT,
    chain_opportunities BIGINT,
    dual_cycle_rate DOUBLE,
    num_trucks BIGINT,
    payload VARCHAR,
    ingested_at TIMESTAMP DEFAULT current_timestamp,
    PRIMARY KEY (run_id, scope, sim_clock, entity_id)
);
"""


def build_run_db(path, run_id: str, count: int = 50, breakdown: int = 0):
    base = datetime(2026, 8, 5, 15, 8, 13)
    conn = duckdb.connect(str(path))
    try:
        conn.execute(SCHEMA)
        for i in range(count):
            conn.execute(
                "INSERT INTO kpi_events (run_id, metric, value, sim_clock) VALUES (?, ?, ?, ?)",
                [run_id, f"metric_{i % 4}", float(i), base + timedelta(minutes=i)],
            )
        for i in range(breakdown):
            conn.execute(
                "INSERT INTO kpi_breakdown_rows (run_id, scope, sim_clock, final, entity_id, "
                "haulier_id, haulier_name, num_orders_completed, empty_km, loaded_km, total_km, "
                "empty_ratio, active_hours, orders_per_day, dual_cycle_count, chain_opportunities, "
                "dual_cycle_rate, num_trucks, payload) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    run_id, "truck", base + timedelta(minutes=i // 5), False, f"truck_{i}",
                    "h1", "Haulier One", i, float(i), 2.0 * i, 3.0 * i, 0.33, 1.5, 2.0,
                    0, 0, 0.0, 1, "{}",
                ],
            )
    finally:
        conn.close()
    return path


def test_run_db_path_uses_the_legacy_filename_mangling(tmp_path):
    assert tool.run_db_path(tmp_path, "run_x").name == "run_x.duckdb"
    assert tool.run_db_path(tmp_path, "run/x").name == "run_x.duckdb"


def test_reader_returns_rows_and_leaves_the_source_untouched(tmp_path):
    run_id = "run_20260805_150813"
    src = build_run_db(tmp_path / f"{run_id}.duckdb", run_id, count=50)
    stat_before = src.stat()
    listing_before = sorted(p.name for p in tmp_path.iterdir())

    rows = tool.read_run_rows(src)

    assert len(rows) == 50
    assert rows[0][0] == run_id
    assert {r[1] for r in rows} == {f"metric_{i}" for i in range(4)}
    assert all(isinstance(r[3], datetime) for r in rows)
    # ascending sim_clock
    assert [r[3] for r in rows] == sorted(r[3] for r in rows)

    stat_after = src.stat()
    assert stat_after.st_size == stat_before.st_size
    assert stat_after.st_mtime == stat_before.st_mtime
    assert sorted(p.name for p in tmp_path.iterdir()) == listing_before


def test_copy_takes_the_wal_sidecar_too_and_cleans_up(tmp_path):
    run_id = "with_wal"
    src = build_run_db(tmp_path / f"{run_id}.duckdb", run_id, count=3)
    wal = pathlib.Path(str(src) + ".wal")
    wal.write_bytes(b"not-a-real-wal")  # presence is what we assert on

    temp_dir, copied = tool.copy_db_to_temp(src)
    try:
        assert copied.parent == temp_dir
        assert copied.exists()
        assert (temp_dir / wal.name).exists()
        assert (temp_dir / wal.name).read_bytes() == b"not-a-real-wal"
        assert temp_dir != src.parent
    finally:
        import shutil

        shutil.rmtree(temp_dir, ignore_errors=True)
    assert wal.exists() and src.exists()


def test_reader_removes_its_temp_dir(tmp_path, monkeypatch):
    run_id = "temp_cleanup"
    src = build_run_db(tmp_path / f"{run_id}.duckdb", run_id, count=2)
    made = []
    real_copy = tool.copy_db_to_temp

    def spy(path):
        temp_dir, copied = real_copy(path)
        made.append(temp_dir)
        return temp_dir, copied

    monkeypatch.setattr(tool, "copy_db_to_temp", spy)
    tool.read_run_rows(src)
    assert made and not made[0].exists()


def test_reader_works_while_another_process_holds_the_lock(tmp_path):
    """The live kpi-duckdb-sink holds these files open — copy-then-open must still work."""
    if not os.path.exists(VENV_PYTHON):  # pragma: no cover
        pytest.skip("venv python not found")
    run_id = "locked_run"
    src = build_run_db(tmp_path / f"{run_id}.duckdb", run_id, count=10)

    holder_src = tmp_path / "holder.py"
    holder_src.write_text(
        "import duckdb, sys, time\n"
        "c = duckdb.connect(sys.argv[1])\n"
        "c.execute('SELECT count(*) FROM kpi_events')\n"
        "print('ready', flush=True)\n"
        "time.sleep(30)\n"
    )
    proc = subprocess.Popen(
        [VENV_PYTHON, str(holder_src), str(src)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.time() + 30
        line = ""
        while time.time() < deadline:
            line = proc.stdout.readline()
            if line.strip() == "ready" or proc.poll() is not None:
                break
        if line.strip() != "ready":
            pytest.skip(f"could not start the lock holder: {proc.stderr.read()[:200]}")

        # opening the original in place is exactly what fails in production
        with pytest.raises(Exception) as excinfo:
            duckdb.connect(str(src), read_only=True)
        assert "lock" in str(excinfo.value).lower()

        # the tool's copy-then-open reader is unaffected
        rows = tool.read_run_rows(src)
        assert len(rows) == 10
    finally:
        proc.kill()
        proc.wait(timeout=10)


def test_dry_run_writes_nothing_and_never_connects_to_mongo(tmp_path, monkeypatch, capsys):
    run_id = "dry"
    src = build_run_db(tmp_path / f"{run_id}.duckdb", run_id, count=6)
    stat_before = src.stat()

    class Exploding:
        def __init__(self, *a, **kw):
            raise AssertionError("dry run must not construct a MongoArchive")

    monkeypatch.setattr(tool, "MongoArchive", Exploding)

    rc = tool.main(["--data-dir", str(tmp_path), "--run-id", run_id, "--dry-run"])
    assert rc == 0

    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert "duckdb_rows=6" in out

    stat_after = src.stat()
    assert (stat_after.st_size, stat_after.st_mtime) == (stat_before.st_size, stat_before.st_mtime)


def test_dry_run_reports_a_missing_run_without_raising(tmp_path, capsys):
    rc = tool.main(["--data-dir", str(tmp_path), "--run-id", "nope", "--dry-run"])
    assert rc == 1
    assert "FAILED" in capsys.readouterr().out


def test_defaults_name_the_two_surviving_runs():
    assert tool.DEFAULT_RUN_IDS == ("run_20260805_150813", "run_20260805_162056")
    parser = tool.build_parser()
    args = parser.parse_args([])
    assert args.run_ids is None  # -> DEFAULT_RUN_IDS in main()
    assert args.dry_run is False
    assert args.also_legacy_kpi is False
    from apps.config import kpi_sink_settings

    assert args.data_dir == kpi_sink_settings["data_dir"]


def test_legacy_doc_shape_matches_kpi_sink_exactly():
    rows = [("r", "m", 2.0, datetime(2026, 8, 5, 12, 0, 0))]
    doc = tool.legacy_kpi_docs(rows)[0]
    assert set(doc) == {"run_id", "metric", "value", "sim_clock", "_created", "_updated"}
    assert doc["value"] == 2.0
    assert doc["_created"] == doc["_updated"] == doc["sim_clock"]


def test_module_only_ever_opens_the_temp_copy():
    """Static guard: every duckdb.connect() targets the copied path, read-only."""
    text = pathlib.Path(tool.__file__).read_text()
    assert text.count("duckdb.connect(") == 1
    assert 'duckdb.connect(str(copied), read_only=True)' in text
    for forbidden in ("CHECKPOINT", "os.remove(", "os.unlink(", "shutil.move(", ".truncate("):
        assert forbidden not in text
    # rmtree is only ever applied to the temp dir
    for line in text.splitlines():
        if "rmtree(" in line:
            assert "temp_dir" in line


def test_loaded_run_is_a_duckreader(tmp_path):
    rows = [("r", "m", 1.0, datetime(2026, 8, 5))]
    reader = tool._LoadedRun("r", rows)
    assert reader.list_run_ids() == ["r"]
    assert list(reader.iter_frames("r")) == []
    assert reader.read_kpi_events("r") == rows
    assert reader.read_kpi_events("other") == []


# ── regressions: the breakdown rows are 97% of these files ──────────────────


class FakeArchive:
    """Captures everything the tool writes. Never a real Mongo connection."""

    def __init__(self):
        self.kpi_rows = []
        self.breakdown_rows = []
        self.summaries = []
        self.legacy_docs = []

        outer = self

        class _Coll:
            def __init__(self, name):
                self.name = name

            def count_documents(self, query):
                if self.name == "kpi":
                    return len(outer.kpi_rows)
                if self.name == "breakdown":
                    return len(outer.breakdown_rows)
                return 0

        self.kpi = _Coll("kpi")
        self.breakdown = _Coll("breakdown")
        self.db = {"kpi": _Coll("legacy")}

    def write_kpi_events(self, run_id, rows, **kw):
        rows = list(rows)
        self.kpi_rows.extend(rows)
        return len(rows)

    def write_breakdown_rows(self, run_id, rows, **kw):
        rows = list(rows)
        self.breakdown_rows.extend(rows)
        return len(rows)

    def insert_documents(self, collection, docs, **kw):
        docs = list(docs)
        self.legacy_docs.extend(docs)
        return len(docs)

    def dump_run(self, *a, **kw):  # pragma: no cover - must never be called
        raise AssertionError("the preservation tool must not write a run summary")


def test_reader_returns_breakdown_rows_too(tmp_path):
    run_id = "both_tables"
    src = build_run_db(tmp_path / f"{run_id}.duckdb", run_id, count=7, breakdown=40)

    tables = tool.read_run_tables(src, run_id)

    assert tables.counts() == {"kpi_events": 7, "kpi_breakdown_rows": 40}
    row = tables.breakdown_rows[0]
    assert row["run_id"] == run_id
    assert row["scope"] == "truck"
    assert row["entity_id"].startswith("truck_")
    assert row["haulier_name"] == "Haulier One"
    assert isinstance(row["sim_clock"], datetime)
    # the back-compat shim still returns only the kpi rows
    assert len(tool.read_run_rows(src)) == 7


def test_reader_tolerates_a_file_without_the_breakdown_table(tmp_path):
    run_id = "kpi_only"
    path = tmp_path / f"{run_id}.duckdb"
    conn = duckdb.connect(str(path))
    try:
        conn.execute(
            "CREATE TABLE kpi_events (run_id VARCHAR, metric VARCHAR, value DOUBLE, "
            "sim_clock TIMESTAMP)"
        )
        conn.execute(
            "INSERT INTO kpi_events VALUES (?, ?, ?, ?)",
            [run_id, "m", 1.0, datetime(2026, 8, 5)],
        )
    finally:
        conn.close()
    tables = tool.read_run_tables(path, run_id)
    assert tables.counts() == {"kpi_events": 1, "kpi_breakdown_rows": 0}


def test_preserve_run_writes_both_tables_and_no_run_summary(tmp_path):
    run_id = "rescue"
    build_run_db(tmp_path / f"{run_id}.duckdb", run_id, count=9, breakdown=25)
    archive = FakeArchive()

    result = tool.preserve_run(archive, tmp_path, run_id)

    assert result["duckdb_kpi_events"] == 9
    assert result["duckdb_breakdown_rows"] == 25
    assert result["breakdown_inserted"] == 25
    assert result["mongo_breakdown_docs"] == 25
    assert len(archive.breakdown_rows) == 25
    assert archive.summaries == []  # dump_run would have raised
    # a zero must be visible in the printed line, not silently absent
    assert "kpi_breakdown_rows duckdb=25" in tool.format_result(result)


def test_preserve_run_reports_a_zero_breakdown_count(tmp_path):
    run_id = "nothing_extra"
    build_run_db(tmp_path / f"{run_id}.duckdb", run_id, count=4, breakdown=0)
    archive = FakeArchive()
    result = tool.preserve_run(archive, tmp_path, run_id)
    assert result["duckdb_breakdown_rows"] == 0
    assert "kpi_breakdown_rows duckdb=0" in tool.format_result(result)


def test_dry_run_reports_the_breakdown_count(tmp_path, monkeypatch, capsys):
    run_id = "dry_bd"
    src = build_run_db(tmp_path / f"{run_id}.duckdb", run_id, count=6, breakdown=17)
    stat_before = src.stat()

    class Exploding:
        def __init__(self, *a, **kw):
            raise AssertionError("dry run must not construct a MongoArchive")

    monkeypatch.setattr(tool, "MongoArchive", Exploding)
    assert tool.main(["--data-dir", str(tmp_path), "--run-id", run_id, "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "kpi_events=6" in out
    assert "kpi_breakdown_rows=17" in out

    stat_after = src.stat()
    assert (stat_after.st_size, stat_after.st_mtime) == (stat_before.st_size, stat_before.st_mtime)


def test_preserve_leaves_the_source_file_untouched(tmp_path):
    run_id = "untouched"
    src = build_run_db(tmp_path / f"{run_id}.duckdb", run_id, count=5, breakdown=11)
    before = (src.stat().st_size, src.stat().st_mtime, sorted(p.name for p in tmp_path.iterdir()))
    tool.preserve_run(FakeArchive(), tmp_path, run_id)
    after = (src.stat().st_size, src.stat().st_mtime, sorted(p.name for p in tmp_path.iterdir()))
    assert before == after


def test_tool_never_calls_dump_run_or_touches_the_runs_collection():
    """Static guard: a partial-shape rescue must not close a run in the archive."""
    text = pathlib.Path(tool.__file__).read_text()
    code = "\n".join(
        line for line in text.splitlines() if not line.strip().startswith(("#", "*"))
    )
    assert "dump_run(" not in code
    assert "archive.runs" not in code
    assert ".replace_one(" not in code
