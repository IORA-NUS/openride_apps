"""FIX-6 / review finding F2 — a pools-authored scenario must survive the editor.

The Scenario tab rebuilds the whole recipe from its form and ``generate_scenario``
then ``rmtree``s the folder, so anything the editor does not understand is deleted.
That destroyed pools: a 3-company consortium hydrated as a clique of pairs and came
back as three 2-company pools — a different market, silently.

These tests drive the REAL functions end to end (generate -> hydrate ->
editor-shaped POST body -> regenerate) and assert on the compiled bundle's
assignment profile, i.e. what the runtime actually reads — not on an intermediate
dict, and with nothing stubbed.
"""

import json
import os
import shutil
import tempfile

import pytest

from apps.container_logistics.scenario.frontend_scenario_spec import (
    _cooperation_from_spec,
    assemble_spec,
    generate_scenario,
    get_scenario_detail,
    read_spec,
    scenario_dir,
)
from apps.container_logistics.scenario.scenario_bundle import load_bundle

DOMAIN = "container-logistics-sim-test"

HAULIERS = [
    {"id": "acme", "name": "Acme", "fleet_share": 25, "order_share": 25},
    {"id": "borax", "name": "Borax", "fleet_share": 25, "order_share": 25},
    {"id": "cargo", "name": "Cargo", "fleet_share": 25, "order_share": 25},
    {"id": "delta", "name": "Delta", "fleet_share": 25, "order_share": 25},
]

# Plan §3.2's consortium: one 3-member pool and one 2-member pool that overlap on
# 'cargo'. Neither is expressible as a set of independent pairs, which is the whole
# point — the edges they derive are a strictly lossy projection.
CONSORTIUM_POOLS = [
    {"id": "north", "members": ["acme", "borax", "cargo"]},
    {"id": "south", "members": ["cargo", "delta"]},
]
CONSORTIUM = {
    "active": "consortium",
    "structures": [{"id": "consortium", "pools": CONSORTIUM_POOLS}],
}
# The clique those pools derive (backend ``derive_edges_from_pools``): this is all
# an edges-only client can ever send back.
CONSORTIUM_EDGES = [
    ["acme", "borax"],
    ["acme", "cargo"],
    ["borax", "cargo"],
    ["cargo", "delta"],
]

EDGES_ONLY = {
    "active": "chain",
    "structures": [{"id": "chain", "edges": [["acme", "borax"], ["borax", "cargo"]]}],
}


@pytest.fixture
def datahub_tmp():
    tmp = tempfile.mkdtemp(prefix="pools_roundtrip_")
    prev = os.environ.get("ORSIM_SCENARIOS_DIR")
    os.environ["ORSIM_SCENARIOS_DIR"] = os.path.join(tmp, DOMAIN, "scenarios")
    yield tmp
    if prev is None:
        os.environ.pop("ORSIM_SCENARIOS_DIR", None)
    else:
        os.environ["ORSIM_SCENARIOS_DIR"] = prev
    shutil.rmtree(tmp, ignore_errors=True)


def _create(datahub: str, slug: str, cooperation) -> None:
    """Author a scenario from scratch (the SpecUpload / CLI path)."""
    generate_scenario(
        datahub,
        DOMAIN,
        {
            "name": slug,
            "slug": slug,
            "simulationDays": 1,
            "orderCountUnit": "total",
            "agents": {
                "truck": {"count": 4},
                "order": {"count": 6},
                "facility": {"count": 3},
            },
            "earlyOrderCount": 0,
            "hauliers": [dict(h) for h in HAULIERS],
            **({"cooperation": cooperation} if cooperation is not None else {}),
        },
        overwrite=True,
    )


def _editor_body(datahub: str, slug: str, cooperation) -> dict:
    """The POST body ``ScenarioTab.persistScenario`` builds, from the hydrated form.

    Mirrors components/scenario/ScenarioTab.tsx:391-431 — every field comes from
    ``editForm``, and ``cooperation`` is whatever ``cooperationForApi`` produced.
    """
    form = get_scenario_detail(datahub, DOMAIN, slug)["editForm"]
    body = {
        "name": form["name"],
        "slug": slug,
        "simulationDays": form["simulationDays"],
        "orderCountUnit": "per_day",
        "agents": {
            "truck": {"count": form["numTrucks"], "policy": {"type": form["policies"]["truck"]}},
            "order": {"count": form["numOrders"], "policy": {"type": form["policies"]["order"]}},
            "facility": {
                "count": form["numFacilities"],
                "policy": {"type": form["policies"]["facility"]},
            },
        },
        "seed": form["seed"],
        "solver": form["solver"],
        "orderDemandCurve": form["orderDemandCurve"],
        "tripMatrix": form["tripMatrix"],
        "roleSettings": form.get("roleSettings"),
        "hauliers": form["hauliers"],
        "overwrite": True,
    }
    if cooperation is not None:
        body["cooperation"] = cooperation
    return body


def _save(datahub: str, slug: str, cooperation, **kw) -> dict:
    """POST the editor body back, exactly as the control plane does.

    ``openride_control.scenario.cmd_generate_scenario`` forwards the body's
    ``overwrite`` flag as the keyword; the key itself stays in the spec dict.
    """
    return generate_scenario(
        datahub, DOMAIN, _editor_body(datahub, slug, cooperation), overwrite=True, **kw
    )


def _compiled_cooperation(datahub: str, slug: str) -> dict:
    """The cooperation block on the compiled assignment profile — runtime truth."""
    collections, _settings, _recipe = load_bundle(scenario_dir(datahub, DOMAIN, slug))
    agent = next(iter(collections["assignment"].values()))
    return agent["profile"]["cooperation"]


def _active(cooperation: dict) -> dict:
    return next(s for s in cooperation["structures"] if s["id"] == cooperation["active"])


# --- layer 1: hydration carries pools, and only for pools authors -------------


def test_hydration_carries_authored_pools(datahub_tmp):
    _create(datahub_tmp, "pools_hydrate", CONSORTIUM)
    spec = read_spec(scenario_dir(datahub_tmp, DOMAIN, "pools_hydrate"))

    hydrated = _cooperation_from_spec(spec)
    structures = {s["id"]: s for s in hydrated["structures"]}
    assert structures["consortium"]["pools"] == CONSORTIUM_POOLS
    # The derived clique is hydrated too (unchanged behaviour), and the
    # auto-inserted baseline was never authored with pools, so it gains no key.
    assert structures["consortium"]["edges"] == CONSORTIUM_EDGES
    assert "pools" not in structures["no-coop"]

    form = get_scenario_detail(datahub_tmp, DOMAIN, "pools_hydrate")["editForm"]
    assert _active(form["cooperation"])["pools"] == CONSORTIUM_POOLS


def test_edges_authored_hydration_is_byte_identical(datahub_tmp):
    """The edges-only flow must be untouched — no 'pools' key anywhere."""
    _create(datahub_tmp, "edges_hydrate", EDGES_ONLY)
    target = scenario_dir(datahub_tmp, DOMAIN, "edges_hydrate")
    spec_before = json.dumps(read_spec(target)["cooperation"], sort_keys=True)

    hydrated = _cooperation_from_spec(read_spec(target))
    assert hydrated == {
        "active": "chain",
        "structures": [
            {"id": "no-coop", "edges": []},
            {"id": "chain", "edges": [["acme", "borax"], ["borax", "cargo"]]},
        ],
    }
    assert all("pools" not in s for s in hydrated["structures"])

    # Full round trip through the editor leaves the persisted recipe unchanged.
    _save(datahub_tmp, "edges_hydrate", hydrated)
    assert json.dumps(read_spec(target)["cooperation"], sort_keys=True) == spec_before
    active = _active(_compiled_cooperation(datahub_tmp, "edges_hydrate"))
    assert active["edges"] == [["acme", "borax"], ["borax", "cargo"]]
    # Edges-authored pools stay the derived one-pool-per-edge form.
    assert active["pools"] == [
        {"id": "p:acme+borax", "members": ["acme", "borax"]},
        {"id": "p:borax+cargo", "members": ["borax", "cargo"]},
    ]


def test_assemble_spec_without_previous_is_unchanged():
    """The new ``previous`` kwarg defaults to a no-op — no previous, no rewrite."""
    raw = {"name": "x", "slug": "x", "cooperation": EDGES_ONLY}
    assert assemble_spec(raw, DOMAIN)["cooperation"] == EDGES_ONLY
    assert assemble_spec({"name": "x"}, DOMAIN)["cooperation"] is None


# --- the headline round trip --------------------------------------------------


def test_pools_authored_spec_survives_editor_round_trip(datahub_tmp):
    """Hydrate -> the editor's POST body -> recompile: the pool MEMBERS survive.

    The pools-aware client (post-FIX-6 ``cooperationForApi``) posts the structure
    back as its pools, with no edges — pools are canonical and the backend derives
    the edges.
    """
    slug = "pools_roundtrip"
    _create(datahub_tmp, slug, CONSORTIUM)
    before = _compiled_cooperation(datahub_tmp, slug)
    assert _active(before)["pools"] == CONSORTIUM_POOLS

    form = get_scenario_detail(datahub_tmp, DOMAIN, slug)["editForm"]
    posted = {
        "active": form["cooperation"]["active"],
        "structures": [
            {"id": s["id"], "pools": s["pools"]} if "pools" in s else {"id": s["id"], "edges": s["edges"]}
            for s in form["cooperation"]["structures"]
        ],
    }
    _save(datahub_tmp, slug, posted)

    after = _compiled_cooperation(datahub_tmp, slug)
    active = _active(after)
    assert active["pools"] == CONSORTIUM_POOLS, "the authored pools were rewritten by the editor"
    # The distinguishing property: 'north' is ONE 3-member pool, not three pairs.
    assert sorted(len(p["members"]) for p in active["pools"]) == [2, 3]
    assert active["edges"] == CONSORTIUM_EDGES
    assert after["active"] == "consortium"
    # Persisted recipe agrees with the compiled profile.
    assert _active(read_spec(scenario_dir(datahub_tmp, DOMAIN, slug))["cooperation"])["pools"] == (
        CONSORTIUM_POOLS
    )


def test_pools_blind_client_body_still_preserves_pools(datahub_tmp):
    """A client that knows nothing about pools posts the derived clique back.

    This is the pre-FIX-6 editor body verbatim, and the exact shape the review's
    probe showed flattening a 3-member pool into 2-member pools. The backend must
    carry the authored pools over from the previous spec.json.
    """
    slug = "pools_blind"
    _create(datahub_tmp, slug, CONSORTIUM)
    form = get_scenario_detail(datahub_tmp, DOMAIN, slug)["editForm"]
    blind = {
        "active": form["cooperation"]["active"],
        # Exactly what the old cooperationForApi built: {id, edges}, no pools.
        "structures": [{"id": s["id"], "edges": s["edges"]} for s in form["cooperation"]["structures"]],
    }
    assert all("pools" not in s for s in blind["structures"])

    _save(datahub_tmp, slug, blind)

    active = _active(_compiled_cooperation(datahub_tmp, slug))
    assert active["pools"] == CONSORTIUM_POOLS
    assert sorted(len(p["members"]) for p in active["pools"]) == [2, 3]


def test_pools_are_not_carried_over_an_explicit_pool_edit(datahub_tmp):
    """A client that DOES speak pools stays authoritative — no silent restore."""
    slug = "pools_explicit"
    _create(datahub_tmp, slug, CONSORTIUM)
    edited = {
        "active": "consortium",
        "structures": [
            {"id": "no-coop", "edges": []},
            {"id": "consortium", "pools": [{"id": "north", "members": ["acme", "borax"]}]},
        ],
    }
    _save(datahub_tmp, slug, edited)
    assert _active(_compiled_cooperation(datahub_tmp, slug))["pools"] == [
        {"id": "north", "members": ["acme", "borax"]}
    ]


# --- layer 2: the destructive-save guard --------------------------------------


def test_generate_scenario_refuses_to_silently_empty_cooperation(datahub_tmp):
    slug = "guard_edges"
    _create(datahub_tmp, slug, EDGES_ONLY)
    target = scenario_dir(datahub_tmp, DOMAIN, slug)
    before = json.dumps(read_spec(target)["cooperation"], sort_keys=True)

    # 1. A client that omits the key entirely (the general failure class).
    with pytest.raises(ValueError) as exc:
        _save(datahub_tmp, slug, None)
    assert "chain" in str(exc.value)
    assert "allow_cooperation_reset" in str(exc.value)

    # 2. A client that sends the structure back emptied.
    emptied = {"active": "chain", "structures": [{"id": "no-coop", "edges": []}, {"id": "chain", "edges": []}]}
    with pytest.raises(ValueError) as exc2:
        _save(datahub_tmp, slug, emptied)
    assert "chain" in str(exc2.value)

    # 3. A client that drops the structure altogether.
    with pytest.raises(ValueError):
        _save(datahub_tmp, slug, {"active": "no-coop", "structures": [{"id": "no-coop", "edges": []}]})

    # Nothing was written on any refusal — the folder is untouched.
    assert json.dumps(read_spec(target)["cooperation"], sort_keys=True) == before
    assert _active(_compiled_cooperation(datahub_tmp, slug))["edges"] == [
        ["acme", "borax"],
        ["borax", "cargo"],
    ]

    # 4. The explicit override still clears it.
    _save(datahub_tmp, slug, emptied, allow_cooperation_reset=True)
    after = _compiled_cooperation(datahub_tmp, slug)
    assert _active(after)["edges"] == []
    assert _active(after)["pools"] == []


def test_guard_catches_a_dropped_pools_structure(datahub_tmp):
    """Pools count as content: a payload with neither edges nor pools is refused."""
    slug = "guard_pools"
    _create(datahub_tmp, slug, CONSORTIUM)
    with pytest.raises(ValueError) as exc:
        _save(
            datahub_tmp,
            slug,
            {"active": "consortium", "structures": [{"id": "consortium", "edges": []}]},
        )
    assert "consortium" in str(exc.value)
    assert _active(_compiled_cooperation(datahub_tmp, slug))["pools"] == CONSORTIUM_POOLS


def test_guard_never_fires_on_a_new_or_coop_less_scenario(datahub_tmp):
    """Creating fresh, and re-saving a scenario that never had cooperation."""
    _create(datahub_tmp, "guard_new", None)  # no previous folder at all
    _create(datahub_tmp, "guard_new", None)  # previous exists, but has no content
    coop = _compiled_cooperation(datahub_tmp, "guard_new")
    assert [s["id"] for s in coop["structures"]] == ["no-coop"]


def test_guard_allows_adding_to_an_existing_structure(datahub_tmp):
    """The normal edges-only edit flow keeps working — content grows, no refusal."""
    slug = "guard_grow"
    _create(datahub_tmp, slug, EDGES_ONLY)
    grown = {
        "active": "chain",
        "structures": [
            {"id": "no-coop", "edges": []},
            {"id": "chain", "edges": [["acme", "borax"], ["borax", "cargo"], ["cargo", "delta"]]},
        ],
    }
    _save(datahub_tmp, slug, grown)
    assert _active(_compiled_cooperation(datahub_tmp, slug))["edges"] == [
        ["acme", "borax"],
        ["borax", "cargo"],
        ["cargo", "delta"],
    ]
