"""The dry run's bundle inputs come from a content-checked sidecar that is never stale (round-2 review M-C).

``scenario.run_inputs.read_run_inputs`` serves the assignment collection and the
settings keys of ``scenario.json`` from ``scenario.run_inputs.json`` -- on a stat match
outside the racy window, or on a SHA-256 match -- and parses the bundle otherwise.
Pinned: every path returns exactly what parsing the bundle returns; an in-place,
SAME-SIZE edit of the bundle (the hardest case for a stat key) is never served from
the old sidecar, even inside the racy window; a forged or corrupt sidecar is ignored; a
read-only folder still works; ``scenario compile`` warms the sidecar, and only for the
file it wrote.

Measured on ``consortium_collab_7d`` (203 MB bundle; ``validate-run-policies`` with no
flag, wall time of the whole subprocess, quiet box): 2.9-3.0 s before, ~0.39 s on a
stat hit, ~0.6 s on a hash hit (the first run after a compile or a touch), ~3.0 s on
the very first read with no sidecar.

Verified to have teeth (mutate / see it fail / restore):

- serving every identity match (dropping the ``RACY_SECONDS`` condition) failed
  ``test_a_same_tick_edit_is_never_served_stale``;
- skipping the hash comparison (``sidecar is not None`` alone) failed it and
  ``test_a_forged_sidecar_is_ignored``;
- dropping the ``createdAt`` check in ``warm_run_inputs`` failed
  ``test_compile_warms_only_its_own_file``.
"""

from __future__ import annotations

import json
import os
import stat

import pytest

from apps.container_logistics.scenario import run_inputs as RI
from apps.container_logistics.scenario.scenario_bundle import read_bundle, write_bundle


def _bundle(n_trucks=3, tag="a"):
    return {
        "schemaVersion": 1, "slug": "s", "createdAt": f"2026-10-23T00:00:00.{tag}",
        "settings": {"STEP_INTERVAL": 240, "DOMAIN": "x", "TAG": tag},
        "agents": {"truck": {f"t{i}": {"profile": {}} for i in range(n_trucks)},
                   "assignment": {"assignment_main": {"profile": {"tag": tag, "solver_params": {"k": 1.5}}}}},
    }


def _expected(folder):
    b = read_bundle(str(folder))
    return b["agents"]["assignment"], tuple(b["settings"])


def _age_sidecar(folder, seconds=10.0):
    """Pretend the sidecar was written ``seconds`` after the bundle last changed (outside the racy window)."""
    path = RI.sidecar_path(str(folder))
    data = json.loads(open(path).read())
    data["written_ns"] = os.stat(os.path.join(folder, "scenario.json")).st_ctime_ns + int(seconds * 1e9)
    open(path, "w").write(json.dumps(data))


def test_parse_then_hash_then_stat_all_return_the_bundle(tmp_path):
    write_bundle(str(tmp_path), _bundle())
    first = RI.read_run_inputs(str(tmp_path))
    assert first.source == "parse" and (first.assignment, first.settings_keys) == _expected(tmp_path)
    second = RI.read_run_inputs(str(tmp_path))      # written inside the racy window: hash-checked
    assert second.source == "hash" and (second.assignment, second.settings_keys) == _expected(tmp_path)
    _age_sidecar(tmp_path)
    third = RI.read_run_inputs(str(tmp_path))
    assert third.source == "stat" and (third.assignment, third.settings_keys) == _expected(tmp_path)


def test_a_same_size_in_place_edit_is_never_served_stale(tmp_path):
    write_bundle(str(tmp_path), _bundle(tag="a"))
    RI.read_run_inputs(str(tmp_path))
    path = tmp_path / "scenario.json"
    raw = path.read_bytes()
    edited = raw.replace(b'"tag": "a"', b'"tag": "b"')
    assert len(edited) == len(raw) and edited != raw
    st = os.stat(path)
    with open(path, "r+b") as fh:                    # in place: same inode, same size
        fh.write(edited)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))  # even with the mtime put back
    got = RI.read_run_inputs(str(tmp_path))
    assert got.assignment["assignment_main"]["profile"]["tag"] == "b", got
    assert got.source == "parse"


def test_a_same_tick_edit_is_never_served_stale(tmp_path):
    """The racy case a stat key alone gets wrong: an edit inside the filesystem's
    timestamp tick leaves the identity unchanged. Simulated by a sidecar that carries
    the CURRENT identity but a stale payload and hash, written within ``RACY_SECONDS``."""
    write_bundle(str(tmp_path), _bundle(tag="new"))
    st = os.stat(tmp_path / "scenario.json")
    stale = {"format": RI.SIDECAR_FORMAT, "bundle": "scenario.json", "sha256": "f" * 64,
             "identity": RI._identity(st), "written_ns": st.st_ctime_ns + int(0.5 * 1e9),
             "assignment": {"assignment_main": {"profile": {"tag": "old"}}}, "settings_keys": ["OLD"]}
    open(RI.sidecar_path(str(tmp_path)), "w").write(json.dumps(stale))
    got = RI.read_run_inputs(str(tmp_path))
    assert got.assignment["assignment_main"]["profile"]["tag"] == "new" and got.source == "parse"
    # Outside the racy window the same identity IS trusted (that is the fast path).
    stale["written_ns"] = st.st_ctime_ns + int(RI.RACY_SECONDS * 1e9) + 1
    open(RI.sidecar_path(str(tmp_path)), "w").write(json.dumps(stale))
    assert RI.read_run_inputs(str(tmp_path)).source == "stat"


def test_a_replaced_bundle_is_reparsed(tmp_path):
    write_bundle(str(tmp_path), _bundle(tag="a"))
    RI.read_run_inputs(str(tmp_path))
    _age_sidecar(tmp_path)
    write_bundle(str(tmp_path), _bundle(tag="c", n_trucks=4))
    got = RI.read_run_inputs(str(tmp_path))
    assert got.source == "parse" and got.assignment["assignment_main"]["profile"]["tag"] == "c"


def test_a_forged_sidecar_is_ignored(tmp_path):
    write_bundle(str(tmp_path), _bundle(tag="a"))
    RI.read_run_inputs(str(tmp_path))
    path = RI.sidecar_path(str(tmp_path))
    data = json.loads(open(path).read())
    data["assignment"] = {"assignment_main": {"profile": {"tag": "FORGED"}}}
    data["sha256"] = "0" * 64   # identity still matches, but inside the racy window -> hash-checked
    open(path, "w").write(json.dumps(data))
    got = RI.read_run_inputs(str(tmp_path))
    assert got.assignment["assignment_main"]["profile"]["tag"] == "a" and got.source == "parse"


@pytest.mark.parametrize("content", ["{not json", "[]", json.dumps({"format": 99}), ""])
def test_a_corrupt_sidecar_is_ignored(tmp_path, content):
    write_bundle(str(tmp_path), _bundle())
    open(RI.sidecar_path(str(tmp_path)), "w").write(content)
    got = RI.read_run_inputs(str(tmp_path))
    assert got.source == "parse" and (got.assignment, got.settings_keys) == _expected(tmp_path)


def test_no_bundle_or_a_bad_bundle_is_none(tmp_path):
    assert RI.read_run_inputs(str(tmp_path)) is None
    (tmp_path / "scenario.json").write_text("{broken")
    assert RI.read_run_inputs(str(tmp_path)) is None
    (tmp_path / "scenario.json").write_text("[1, 2]")
    assert RI.read_run_inputs(str(tmp_path)) is None


def test_a_read_only_folder_still_works(tmp_path):
    write_bundle(str(tmp_path), _bundle())
    os.chmod(tmp_path, stat.S_IRUSR | stat.S_IXUSR)
    try:
        got = RI.read_run_inputs(str(tmp_path))
        assert got.source == "parse" and (got.assignment, got.settings_keys) == _expected(tmp_path)
        assert not os.path.exists(RI.sidecar_path(str(tmp_path)))
    finally:
        os.chmod(tmp_path, stat.S_IRWXU)


def test_compile_warms_only_its_own_file(tmp_path):
    ours = _bundle(tag="a")
    write_bundle(str(tmp_path), ours)
    assert RI.warm_run_inputs(str(tmp_path), ours) is True
    got = RI.read_run_inputs(str(tmp_path))
    assert got.source == "hash" and (got.assignment, got.settings_keys) == _expected(tmp_path)
    # Someone else's bundle replaced ours before the warm-up: nothing is written.
    os.unlink(RI.sidecar_path(str(tmp_path)))
    write_bundle(str(tmp_path), _bundle(tag="z"))
    assert RI.warm_run_inputs(str(tmp_path), ours) is False
    assert not os.path.exists(RI.sidecar_path(str(tmp_path)))


def test_the_compiled_smoke_bundle_is_warmed_and_matches(tmp_path):
    from pathlib import Path

    from apps.container_logistics.scenario.spec_compile import compile_spec_to_bundle

    spec = json.loads((Path(__file__).resolve().parents[1] / "scenarios" / "collab_smoke_12t" / "spec.json").read_text())
    folder = tmp_path / spec["slug"]
    folder.mkdir()
    compile_spec_to_bundle(spec, domain="container-logistics-sim", scenario_dir=str(folder), slug=spec["slug"])
    assert os.path.isfile(RI.sidecar_path(str(folder)))
    got = RI.read_run_inputs(str(folder))
    assert got.source == "hash" and (got.assignment, got.settings_keys) == _expected(folder)


def test_the_control_plane_reads_through_it(tmp_path, monkeypatch):
    from openride_control.policies import load_assignment_collection

    monkeypatch.setenv("ORSIM_SCENARIOS_DIR", str(tmp_path))
    write_bundle(str(tmp_path / "s"), _bundle(tag="q"))
    assert load_assignment_collection("s") == read_bundle(str(tmp_path / "s"))["agents"]["assignment"]
    assert os.path.isfile(RI.sidecar_path(str(tmp_path / "s")))
    # The sidecar is a cache only: deleting it changes nothing.
    os.unlink(RI.sidecar_path(str(tmp_path / "s")))
    assert load_assignment_collection("s")["assignment_main"]["profile"]["tag"] == "q"
