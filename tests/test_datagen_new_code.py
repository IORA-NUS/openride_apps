"""A brand-new location code (here ``RL``) that appears in the address book and the
trip matrix must flow through data generation end-to-end with NO edits to the
datagen package — codes are discovered from data, metadata/labels are derived."""

import random
from collections import Counter

from apps.container_logistics.datagen import (
    CodeRegistry,
    LocationCatalog,
    ScenarioGenerator,
    GenerationSpec,
    parse_trip_matrix,
    restrict_trip_matrix,
)


def _write_csv(path):
    # Real-ish Singapore coordinates for four codes, including a NEW code "RL".
    rows = [
        ("CT", 103.70, 1.26), ("CT", 103.72, 1.27),
        ("CU", 103.85, 1.30), ("CU", 103.86, 1.29), ("CU", 103.84, 1.31),
        ("MT", 103.78, 1.33), ("MT", 103.80, 1.34),
        ("RL", 103.76, 1.35), ("RL", 103.77, 1.36),   # <-- never seen by datagen
        ("YD", 103.90, 1.32),                          # excluded
    ]
    with open(path, "w") as fp:
        fp.write("postal_code,code,address,lon,lat\n")
        for i, (code, lon, lat) in enumerate(rows):
            fp.write(f"{i:05d},{code},addr {i},{lon},{lat}\n")
    return str(path)


def test_new_code_flows_end_to_end(tmp_path):
    csv = _write_csv(tmp_path / "locations.csv")

    # YD excluded; every other code (incl. the new RL) discovered from the CSV.
    catalog = LocationCatalog(csv, excluded=("YD",))
    assert set(catalog.codes()) == {"CT", "CU", "MT", "RL"}

    # A trip matrix that *uses* RL. No code list is hardcoded anywhere.
    raw_matrix = {
        "CT": {"CU": 5, "RL": 5},
        "CU": {"MT": 5, "RL": 5},
        "RL": {"CU": 5, "MT": 5},
        "MT": {"CU": 5},
        "YD": {"CU": 99},  # excluded -> dropped on restrict
    }
    matrix = restrict_trip_matrix(parse_trip_matrix(raw_matrix), catalog.codes())
    assert "YD" not in matrix and "RL" in matrix

    # Only CT gets a friendly label; RL has NO metadata -> derived label "Rl".
    registry = CodeRegistry.build(
        catalog.codes(), matrix, metadata={"CT": {"label": "Port", "prefix": "port"}}
    )
    assert registry.label("CT") == "Port"
    assert registry.label("RL") == "Rl"          # derived, no config needed
    assert registry.prefix("RL") == "rl"
    assert registry.weight("RL") > 0             # demand-proportional from the matrix

    facilities = catalog.facility_sites(20, registry, gate_count=1, service_time=1800)
    fac_codes = Counter(f["code"] for f in facilities)
    assert fac_codes["RL"] >= 1                   # new code gets its own facilities
    assert all(f["facility_type"] == registry.label(f["code"]) for f in facilities)
    assert all(f["name"].startswith(registry.prefix(f["code"])) for f in facilities)

    spec = GenerationSpec(
        domain="d", num_trucks=5, num_orders=400, num_facilities=len(facilities),
        simulation_days=1, step_interval_seconds=240, simulation_length_in_steps=360,
        facility_settings={"profile": {"facilities": facilities}},
        trip_matrix=matrix, excluded_codes=("YD",),
    )
    result = ScenarioGenerator(spec, catalog, rng=random.Random(7)).generate()

    # RL shows up as a real order endpoint, fully coherent, never a sea/ghost point.
    rl_orders = [
        o for o in result.order.values()
        if "RL" in (o["pickup_code"], o["delivery_code"])
    ]
    assert rl_orders, "new RL code never appeared in any order"
    for o in result.order.values():
        assert o["pickup_facility"]["code"] == o["pickup_code"]
        assert o["dropoff_facility"]["code"] == o["delivery_code"]
        assert o["pickup_location_type"] == registry.label(o["pickup_code"])
        assert "YD" not in (o["pickup_code"], o["delivery_code"])
