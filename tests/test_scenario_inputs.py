"""Parsing uploaded trip-matrix / demand-curve files (csv/xlsx/json) at spec creation.

The file is parsed and **inlined** into the spec — no file reference is persisted. All
entry points end up with the same normalized inline value as a hand-inlined matrix/curve.
"""

import os
import shutil
import tempfile

import pytest

from apps.container_logistics.scenario import frontend_scenario_spec as fe
from apps.container_logistics.scenario import scenario_inputs as si

DOMAIN = "container-logistics-sim-test"

INLINE_MATRIX = {"CT": {"CU": 15, "MT": 4}, "CU": {"CT": 11, "MT": 14}, "MT": {"CT": 3, "CU": 14}}


@pytest.fixture
def tmpdir():
    d = tempfile.mkdtemp(prefix="scenario_inputs_")
    yield d
    shutil.rmtree(d, ignore_errors=True)


def _write(path: str, text: str) -> str:
    with open(path, "w", encoding="utf-8") as fp:
        fp.write(text)
    return path


# ---------------------------------------------------------------- matrix parsing

def test_matrix_csv_equals_inline(tmpdir):
    csv_path = _write(
        os.path.join(tmpdir, "m.csv"),
        "pickup,CT,CU,MT\nCT,0,15,4\nCU,11,0,14\nMT,3,14,0\n",
    )
    grid = si.parse_trip_matrix_file(csv_path)
    assert fe.parse_trip_matrix(grid) == fe.parse_trip_matrix(INLINE_MATRIX)


def test_matrix_xlsx_equals_inline(tmpdir):
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(["pickup", "CT", "CU", "MT"])
    ws.append(["CT", 0, 15, 4])
    ws.append(["CU", 11, 0, 14])
    ws.append(["MT", 3, 14, 0])
    xlsx_path = os.path.join(tmpdir, "m.xlsx")
    wb.save(xlsx_path)
    grid = si.parse_trip_matrix_file(xlsx_path)
    assert fe.parse_trip_matrix(grid) == fe.parse_trip_matrix(INLINE_MATRIX)


def test_curve_csv_parses(tmpdir):
    rows = "hour,weight\n" + "".join(f"{h},{1.0 if h == 9 else 0.1}\n" for h in range(24))
    csv_path = _write(os.path.join(tmpdir, "c.csv"), rows)
    curve = si.parse_demand_curve_file(csv_path)
    assert curve["resolution"] == "hour"
    assert {p["hour"] for p in curve["points"]} == set(range(24))


def test_bad_matrix_layout_raises(tmpdir):
    bad = _write(os.path.join(tmpdir, "bad.csv"), "just one row\n")
    with pytest.raises(ValueError):
        si.parse_trip_matrix_file(bad)


# ------------------------------------------------------- $file resolution + inline

def test_maybe_parse_passthrough_inline():
    # A plain inline dict is returned unchanged (no $file).
    assert si.maybe_parse_spec_file(INLINE_MATRIX, "matrix") is INLINE_MATRIX


def test_normalize_file_equals_inline(tmpdir):
    csv_path = _write(
        os.path.join(tmpdir, "m.csv"),
        "pickup,CT,CU,MT\nCT,0,15,4\nCU,11,0,14\nMT,3,14,0\n",
    )
    base = {
        "name": "F",
        "slug": "f_test",
        "simulationDays": 1,
        "agents": {"truck": {"count": 2}, "order": {"count": 4}, "facility": {"count": 2}},
    }
    from_file = fe.normalize_generate_spec({**base, "tripMatrix": {"$file": csv_path}})
    from_inline = fe.normalize_generate_spec({**base, "tripMatrix": INLINE_MATRIX})
    assert from_file["tripMatrix"] == from_inline["tripMatrix"]
    # The normalized spec carries the matrix INLINE — no $file ref persisted.
    assert "$file" not in str(from_file["tripMatrix"])


def test_generate_inlines_file_matrix(tmpdir, monkeypatch):
    # End-to-end through generate: spec.json must contain the inline matrix, no $file.
    scenarios = os.path.join(tmpdir, DOMAIN, "scenarios")
    monkeypatch.setenv("ORSIM_SCENARIOS_DIR", scenarios)
    csv_path = _write(
        os.path.join(tmpdir, "m.csv"),
        "pickup,CT,CU,MT\nCT,0,1,0\nCU,0,0,0\nMT,0,0,0\n",  # all CT->CU
    )
    fe.generate_scenario(
        tmpdir,
        DOMAIN,
        {
            "name": "File Matrix",
            "slug": "file_matrix",
            "simulationDays": 1,
            "orderCountUnit": "total",
            "agents": {"truck": {"count": 2}, "order": {"count": 6}, "facility": {"count": 2}},
            "earlyOrderCount": 0,
            "tripMatrix": {"$file": csv_path},
        },
        overwrite=True,
    )
    spec = fe.read_spec(fe.scenario_dir(tmpdir, DOMAIN, "file_matrix"))
    assert "$file" not in str(spec)
    assert spec["tripMatrix"]["CT"]["CU"] == pytest.approx(1.0)
    # Orders follow the file's matrix (every order CT->CU).
    from apps.container_logistics.scenario import scenario_bundle

    coll, _settings, _recipe = scenario_bundle.load_bundle(fe.scenario_dir(tmpdir, DOMAIN, "file_matrix"))
    for order in coll["order"].values():
        assert order["pickup_code"] == "CT"
        assert order["delivery_code"] == "CU"
