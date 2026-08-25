"""Cooperation structures + planner config (collaboration plan A1 gate).

Covers: normalization of the supervisor's edge-list format (dupes, reversed pairs,
self-loops), validation errors (unknown member, bad active, bad planner axes),
profile/recipe baking, I1 (cooperation is inert at generation — byte-identical
agents), I3 (defaults == today), and recipe round-trip (I4).
"""

import json
from copy import deepcopy

import pytest

from apps.container_logistics.datagen import hauliers as H
from apps.container_logistics.datagen.generator import ScenarioGenerator
from apps.container_logistics.datagen.preprocess import Preprocessor, SpecValidationError

DOMAIN = "container-logistics-sim-test"

SIX = [
    {"id": "18ad362ec7f6aeb34432f1290f43ec28", "name": "A", "fleet_share": 20, "order_share": 20},
    {"id": "fcbe8573710f443597f7f8cd19b8c7c4", "name": "B", "fleet_share": 20, "order_share": 20},
    {"id": "579473f710a782b94c4f7be12fb28ffd", "name": "C", "fleet_share": 15, "order_share": 15},
    {"id": "c2e7ab0e5cc7fef0039477946a9e9322", "name": "D", "fleet_share": 15, "order_share": 15},
    {"id": "c8b50a081311c9f57769693788508f0e", "name": "E", "fleet_share": 15, "order_share": 15},
    {"id": "cff611be4438762f9cacb6027288f397", "name": "F", "fleet_share": 15, "order_share": 15},
]
A, B, C, D, E, F = [h["id"] for h in SIX]

# The supervisor's reference structure verbatim: self-loops + unordered pairs.
SUPERVISOR_EDGES = [
    [A, A], [A, B], [C, C], [D, D], [E, D], [E, E], [F, C], [F, F], [B, B],
]


def _spec(**kw):
    spec = {
        "name": "Coop Test",
        "slug": "coop_test",
        "simulationDays": 1,
        "orderCountUnit": "total",
        "agents": {"truck": {"count": 6}, "order": {"count": 12}, "facility": {"count": 4}},
        "earlyOrderCount": 0,
        "hauliers": deepcopy(SIX),
    }
    spec.update(kw)
    return spec


# --- normalization -----------------------------------------------------------

def test_supervisor_edge_list_normalizes_to_three_pairs():
    coop = H.normalize_cooperation(
        {"structures": [{"id": "three-pairs", "edges": SUPERVISOR_EDGES}]},
        [h["id"] for h in SIX],
    )
    struct = H.active_structure(coop)
    # 'active' defaults to the first DECLARED structure; a no-edge baseline is
    # auto-inserted but must not steal the default.
    assert struct["id"] == "three-pairs"
    assert struct["edges"] == sorted([sorted([A, B]), sorted([D, E]), sorted([C, F])])
    assert struct["adjacency"][A] == [B] and struct["adjacency"][B] == [A]
    assert struct["adjacency"][E] == [D] and struct["adjacency"][F] == [C]
    # Components: three pairs, nothing transitive, all six covered.
    assert struct["components"] == sorted(
        [sorted([A, B]), sorted([C, F]), sorted([D, E])]
    )
    # The auto-inserted baseline exists and has no edges.
    ids = [s["id"] for s in coop["structures"]]
    assert H.DEFAULT_STRUCTURE_ID in ids


def test_bare_edge_list_is_one_anonymous_structure():
    coop = H.normalize_cooperation(SUPERVISOR_EDGES, [h["id"] for h in SIX])
    struct = H.active_structure(coop)
    assert len(struct["edges"]) == 3


def test_absent_cooperation_is_no_coop_baseline():
    coop = H.normalize_cooperation(None, [h["id"] for h in SIX])
    struct = H.active_structure(coop)
    assert struct["id"] == H.DEFAULT_STRUCTURE_ID
    assert struct["edges"] == [] and struct["adjacency"] == {}
    assert struct["components"] == [[hid] for hid in sorted([A, B, C, D, E, F])]


def test_active_by_index_and_id():
    raw = {
        "active": 1,
        "structures": [
            {"id": "s-one", "edges": [[A, B]]},
            {"id": "s-two", "edges": [[C, F]]},
        ],
    }
    ids = [h["id"] for h in SIX]
    assert H.normalize_cooperation(raw, ids)["active"] == "s-two"
    raw["active"] = "s-one"
    assert H.normalize_cooperation(raw, ids)["active"] == "s-one"


# --- validation errors -------------------------------------------------------

def test_unknown_member_rejected():
    with pytest.raises(ValueError, match="unknown haulier id"):
        H.normalize_cooperation([[A, "nobody"]], [h["id"] for h in SIX])


def test_bad_active_rejected():
    ids = [h["id"] for h in SIX]
    with pytest.raises(ValueError, match="out of range"):
        H.normalize_cooperation({"active": 5, "structures": [[[A, B]]]}, ids)
    with pytest.raises(ValueError, match="not found"):
        H.normalize_cooperation({"active": "ghost", "structures": [[[A, B]]]}, ids)


def test_malformed_edge_rejected():
    with pytest.raises(ValueError, match="2-element"):
        H.normalize_cooperation([[A, B, C]], [h["id"] for h in SIX])


def test_preprocessor_maps_cooperation_errors_to_spec_validation():
    with pytest.raises(SpecValidationError, match="unknown haulier id"):
        Preprocessor.compile(_spec(cooperation=[[A, "ghost_co"]]), domain=DOMAIN)


def test_preprocessor_rejects_bad_planner_axes():
    with pytest.raises(SpecValidationError, match="unknown topology"):
        Preprocessor.compile(_spec(planner={"topology": "swarm"}), domain=DOMAIN)
    with pytest.raises(SpecValidationError, match="unknown timing mode"):
        Preprocessor.compile(_spec(planner={"timing": "yearly"}), domain=DOMAIN)


# --- profile bake + recipe ---------------------------------------------------

def test_profile_carries_cooperation_and_planner():
    spec = _spec(
        cooperation={"active": "three-pairs",
                     "structures": [{"id": "three-pairs", "edges": SUPERVISOR_EDGES}]},
        planner={"topology": "two-stage", "timing": {"mode": "online"}},
        solver="GreedyNearest",
    )
    compiled = Preprocessor.compile(spec, domain=DOMAIN)
    profile = compiled.spec.assignment_settings["profile"]
    coop = profile["cooperation"]
    assert coop["active"] == "three-pairs"
    assert H.active_structure(coop)["adjacency"][A] == [B]
    planner = profile["planner"]
    # 'two-stage' is a deprecated authoring alias — resolved to the canonical
    # 'pooled' at compile so the runtime never sees the alias (plan §6.10).
    assert planner["topology"] == "pooled"
    assert planner["timing"]["mode"] == "online"
    # deployment mirrors the resolved strategy; two-stage gets the default sharing algo.
    assert planner["deployment"]["type"] == "GreedyNearest" == profile["strategy"]
    assert planner["sharing"]["type"] == "transfer-when-cheaper"


def test_defaults_reproduce_today(  # I3
):
    compiled = Preprocessor.compile(_spec(), domain=DOMAIN)
    profile = compiled.spec.assignment_settings["profile"]
    struct = H.active_structure(profile["cooperation"])
    assert struct["edges"] == []
    planner = profile["planner"]
    assert planner["topology"] == "partitioned"  # pools/market never change the default
    assert planner["timing"] == {"mode": "online"}
    assert planner["sharing"] is None
    assert planner["deployment"]["type"] == profile["strategy"]


def test_recipe_round_trips():  # I4
    spec = _spec(
        cooperation={"structures": [{"id": "three-pairs", "edges": SUPERVISOR_EDGES}]},
        planner={"topology": "partitioned"},
    )
    first = Preprocessor.compile(spec, domain=DOMAIN)
    recipe = json.loads(json.dumps(first.recipe))  # simulate spec.json persistence
    assert recipe["cooperation"]["active"] == "three-pairs"
    assert all("adjacency" not in s for s in recipe["cooperation"]["structures"])
    # An EDGES-authored spec gains no 'pools' key: the recipe echoes only what the
    # author wrote, so edges-authored scenarios round-trip byte-identically.
    assert all("pools" not in s for s in recipe["cooperation"]["structures"])
    second = Preprocessor.compile(recipe, domain=DOMAIN)
    p1 = first.spec.assignment_settings["profile"]
    p2 = second.spec.assignment_settings["profile"]
    assert p1["cooperation"] == p2["cooperation"]
    assert p1["planner"] == p2["planner"]
    # The recipe is a fixed point: recompiling it yields the same recipe.
    assert json.loads(json.dumps(second.recipe)) == recipe

    # A POOLS-authored spec keeps its pools in the recipe (and still round-trips).
    pooled_spec = _spec(
        cooperation={
            "active": "consortium",
            "structures": [
                {"id": "consortium", "pools": [{"id": "north", "members": [A, B, C]}]}
            ],
        }
    )
    p_first = Preprocessor.compile(pooled_spec, domain=DOMAIN)
    p_recipe = json.loads(json.dumps(p_first.recipe))
    structs = {s["id"]: s for s in p_recipe["cooperation"]["structures"]}
    assert structs["consortium"]["pools"] == [{"id": "north", "members": sorted([A, B, C])}]
    assert "pools" not in structs["no-coop"]  # auto-inserted baseline was not authored
    p_second = Preprocessor.compile(p_recipe, domain=DOMAIN)
    assert (
        p_first.spec.assignment_settings["profile"]["cooperation"]
        == p_second.spec.assignment_settings["profile"]["cooperation"]
    )
    assert json.loads(json.dumps(p_second.recipe)) == p_recipe


def _generated_collections(spec_raw):
    compiled = Preprocessor.compile(spec_raw, domain=DOMAIN)
    result = ScenarioGenerator(compiled.spec).generate()
    # Compare everything EXCEPT the assignment behavior (which intentionally
    # carries the cooperation/planner config).
    return {"truck": result.truck, "order": result.order, "facility": result.facility}, result


def test_cooperation_is_inert_at_generation():  # I1 / I-P6 — the fairness invariant
    base, _ = _generated_collections(_spec())
    coop, _ = _generated_collections(
        _spec(
            cooperation={"structures": [{"id": "three-pairs", "edges": SUPERVISOR_EDGES}]},
            planner={"topology": "two-stage"},
        )
    )
    assert json.dumps(base, sort_keys=True, default=str) == json.dumps(
        coop, sort_keys=True, default=str
    ), "cooperation/planner config must never perturb generated agents"

    # I-P6: pools + a market block are equally inert.
    pooled, _ = _generated_collections(
        _spec(
            cooperation={
                "active": "consortium",
                "structures": [
                    {"id": "no-coop", "pools": []},
                    {
                        "id": "consortium",
                        "pools": [
                            {"id": "north", "members": [A, B, C]},
                            {"id": "south", "members": [C, D]},
                        ],
                    },
                ],
            },
            planner={
                "topology": "pooled",
                "market": {
                    "offer": {"type": "OfferAll", "params": {}},
                    "claim": {"type": "ClaimAllPlanned", "params": {"min_gain_km": 0.0}},
                    "arbitration": {"type": "LowestCost", "params": {}},
                    "max_rounds": 3,
                },
            },
        )
    )
    assert json.dumps(base, sort_keys=True, default=str) == json.dumps(
        pooled, sort_keys=True, default=str
    ), "pools/market config must never perturb generated agents"


def test_planner_deployment_params_survive_compile():
    # F4 regression: authored deployment params must land in solver_params and
    # echo back in the recipe (recipe == what runs), with top-level solverParams
    # taking precedence.
    spec = _spec(
        planner={"deployment": {"type": "GreedyNearest",
                                "params": {"dual_cycle_bonus_km": 10.0}}},
    )
    compiled = Preprocessor.compile(spec, domain=DOMAIN)
    prof = compiled.spec.assignment_settings["profile"]
    assert prof["strategy"] == "GreedyNearest"
    assert prof["solver_params"]["dual_cycle_bonus_km"] == 10.0
    assert compiled.recipe["planner"]["deployment"]["params"]["dual_cycle_bonus_km"] == 10.0
    # Top-level solverParams wins over deployment params.
    spec2 = _spec(
        planner={"deployment": {"type": "GreedyNearest",
                                "params": {"dual_cycle_bonus_km": 10.0}}},
        solverParams={"dual_cycle_bonus_km": 3.0},
    )
    prof2 = Preprocessor.compile(spec2, domain=DOMAIN).spec.assignment_settings["profile"]
    assert prof2["solver_params"]["dual_cycle_bonus_km"] == 3.0
