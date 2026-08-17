"""Shared pools + the planner ``market`` block (shared-pool plan §9, Phase 1).

Pools are the canonical sharing primitive, edges are authoring sugar: each form
derives the other deterministically, and authoring both is only legal when they
agree. ``adjacency``/``components`` are still derived from ``edges`` — these tests
lock that they are unchanged by the pools work.
"""

import pytest

from apps.container_logistics.datagen import hauliers as H
from apps.container_logistics.datagen.preprocess import (
    DEFAULT_MARKET,
    Preprocessor,
    SpecValidationError,
)

DOMAIN = "container-logistics-sim-test"

FOUR = [
    {"id": "acme", "name": "Acme", "fleet_share": 25, "order_share": 25},
    {"id": "borax", "name": "Borax", "fleet_share": 25, "order_share": 25},
    {"id": "cargo", "name": "Cargo", "fleet_share": 25, "order_share": 25},
    {"id": "delta", "name": "Delta", "fleet_share": 25, "order_share": 25},
]
IDS = [h["id"] for h in FOUR]


def _spec(**kw):
    spec = {
        "name": "Pool Test",
        "slug": "pool_test",
        "simulationDays": 1,
        "orderCountUnit": "total",
        "agents": {"truck": {"count": 4}, "order": {"count": 8}, "facility": {"count": 4}},
        "earlyOrderCount": 0,
        "hauliers": [dict(h) for h in FOUR],
    }
    spec.update(kw)
    return spec


# --- edges <-> pools ---------------------------------------------------------

def test_edges_derive_one_pool_per_edge():
    coop = H.normalize_cooperation(
        {"structures": [{"id": "chain", "edges": [["acme", "borax"], ["borax", "cargo"]]}]},
        IDS,
    )
    struct = H.active_structure(coop)
    assert struct["id"] == "chain"
    assert struct["pools"] == [
        {"id": "p:acme+borax", "members": ["acme", "borax"]},
        {"id": "p:borax+cargo", "members": ["borax", "cargo"]},
    ]
    # Edge-direct, non-transitive: acme and cargo never share a pool.
    assert not any({"acme", "cargo"} <= set(p["members"]) for p in struct["pools"])
    # Edges untouched; adjacency/components still derived from edges.
    assert struct["edges"] == [["acme", "borax"], ["borax", "cargo"]]
    assert struct["adjacency"]["borax"] == ["acme", "cargo"]
    # The auto-inserted no-edge baseline carries an empty pool list.
    baseline = [s for s in coop["structures"] if s["id"] == H.DEFAULT_STRUCTURE_ID][0]
    assert baseline["pools"] == []


def test_authored_pools_derive_edges():
    coop = H.normalize_cooperation(
        {
            "active": "consortium",
            "structures": [
                {
                    "id": "consortium",
                    "pools": [{"id": "north", "members": ["cargo", "acme", "borax"]}],
                }
            ],
        },
        IDS,
    )
    struct = H.active_structure(coop)
    assert struct["pools"] == [{"id": "north", "members": ["acme", "borax", "cargo"]}]
    # All member pairs, canonical [min, max], deduped, sorted.
    assert struct["edges"] == [
        ["acme", "borax"],
        ["acme", "cargo"],
        ["borax", "cargo"],
    ]
    assert struct["adjacency"] == {
        "acme": ["borax", "cargo"],
        "borax": ["acme", "cargo"],
        "cargo": ["acme", "borax"],
    }
    assert struct["components"] == [["acme", "borax", "cargo"], ["delta"]]
    # Round-trip: the derived edges regenerate the same pair set.
    assert H.derive_edges_from_pools(H.derive_pools_from_edges(struct["edges"])) == struct["edges"]


def test_pools_and_edges_disagreement_rejected():
    raw = {
        "structures": [
            {
                "id": "mismatch",
                "pools": [{"members": ["acme", "borax"]}],
                "edges": [["borax", "cargo"]],
            }
        ]
    }
    with pytest.raises(ValueError, match="disagree"):
        H.normalize_cooperation(raw, IDS)

    # Agreement is fine: the same relation authored twice compiles.
    ok = H.normalize_cooperation(
        {
            "structures": [
                {
                    "id": "agree",
                    "pools": [{"members": ["acme", "borax"]}],
                    "edges": [["borax", "acme"]],  # reversed, still the same edge
                }
            ]
        },
        IDS,
    )
    assert H.active_structure(ok)["edges"] == [["acme", "borax"]]


def test_unknown_pool_member_rejected():
    raw = {"structures": [{"id": "bad", "pools": [{"members": ["acme", "nobody"]}]}]}
    with pytest.raises(ValueError, match="unknown haulier id"):
        H.normalize_cooperation(raw, IDS)


def test_pool_under_two_members_dropped():
    raw = {
        "structures": [
            {
                "id": "sparse",
                "pools": [
                    {"id": "solo", "members": ["acme"]},          # < 2 members
                    {"id": "empty", "members": []},               # no members
                    {"id": "dupe", "members": ["borax", "borax"]},  # < 2 DISTINCT members
                    {"id": "pair", "members": ["acme", "borax"]},
                ],
            }
        ]
    }
    struct = H.active_structure(H.normalize_cooperation(raw, IDS))
    # Dropped silently, exactly like a self-loop edge.
    assert struct["pools"] == [{"id": "pair", "members": ["acme", "borax"]}]
    assert struct["edges"] == [["acme", "borax"]]


def test_pool_ids_and_members_sorted_and_deduped():
    raw = {
        "structures": [
            {
                "id": "dupes",
                "pools": [
                    {"id": "Zulu Pool", "members": ["cargo", "acme", "acme"]},
                    {"id": "north", "members": ["acme", "borax"]},
                    {"id": "north", "members": ["borax", "cargo"]},
                    {"members": ["cargo", "delta"]},  # id derived
                ],
            }
        ]
    }
    struct = H.active_structure(H.normalize_cooperation(raw, IDS))
    assert struct["pools"] == [
        {"id": "north", "members": ["acme", "borax"]},
        {"id": "north-1", "members": ["borax", "cargo"]},
        {"id": "p:cargo+delta", "members": ["cargo", "delta"]},
        {"id": "zulu-pool", "members": ["acme", "cargo"]},
    ]
    # Sorted by id, members sorted + deduped.
    assert [p["id"] for p in struct["pools"]] == sorted(p["id"] for p in struct["pools"])
    for pool in struct["pools"]:
        assert pool["members"] == sorted(set(pool["members"]))


# --- planner market block ----------------------------------------------------

def test_market_block_defaults_and_shape_validation():
    # Defaulted and always emitted, on the baked profile and in the recipe.
    compiled = Preprocessor.compile(_spec(), domain=DOMAIN)
    planner = compiled.spec.assignment_settings["profile"]["planner"]
    assert planner["market"] == DEFAULT_MARKET
    assert compiled.recipe["planner"]["market"] == DEFAULT_MARKET

    # Authored values survive; missing params default to {}.
    compiled2 = Preprocessor.compile(
        _spec(planner={"market": {"offer": {"type": "OfferCheapest"}, "max_rounds": 5}}),
        domain=DOMAIN,
    )
    market = compiled2.spec.assignment_settings["profile"]["planner"]["market"]
    assert market["offer"] == {"type": "OfferCheapest", "params": {}}
    assert market["claim"] == DEFAULT_MARKET["claim"]
    assert market["arbitration"] == DEFAULT_MARKET["arbitration"]
    assert market["max_rounds"] == 5

    # Shape errors.
    with pytest.raises(SpecValidationError, match="max_rounds"):
        Preprocessor.compile(_spec(planner={"market": {"max_rounds": 0}}), domain=DOMAIN)
    # Ceiling is 100, not 10 (plan §13.8 errata): the old ceiling sat AT the
    # observed convergence point, so an operator who noticed the truncation could
    # not configure their way out of it. 11 must now be ACCEPTED.
    Preprocessor.compile(_spec(planner={"market": {"max_rounds": 11}}), domain=DOMAIN)
    with pytest.raises(SpecValidationError, match="max_rounds"):
        Preprocessor.compile(_spec(planner={"market": {"max_rounds": 101}}), domain=DOMAIN)
    with pytest.raises(SpecValidationError, match="max_rounds"):
        Preprocessor.compile(_spec(planner={"market": {"max_rounds": "two"}}), domain=DOMAIN)
    with pytest.raises(SpecValidationError, match="market"):
        Preprocessor.compile(_spec(planner={"market": []}), domain=DOMAIN)
    with pytest.raises(SpecValidationError, match="params"):
        Preprocessor.compile(
            _spec(planner={"market": {"claim": {"type": "ClaimAllPlanned", "params": 3}}}),
            domain=DOMAIN,
        )
    with pytest.raises(SpecValidationError, match="type"):
        Preprocessor.compile(
            _spec(planner={"market": {"arbitration": {"type": 7}}}), domain=DOMAIN
        )

    # Algorithm NAMES stay fail-soft: an unknown policy name compiles fine.
    unknown = Preprocessor.compile(
        _spec(planner={"market": {"arbitration": {"type": "NoSuchRule"}}}), domain=DOMAIN
    )
    assert (
        unknown.spec.assignment_settings["profile"]["planner"]["market"]["arbitration"]["type"]
        == "NoSuchRule"
    )


def test_pooled_topology_accepted_two_stage_aliases():
    pooled = Preprocessor.compile(_spec(planner={"topology": "pooled"}), domain=DOMAIN)
    planner = pooled.spec.assignment_settings["profile"]["planner"]
    assert planner["topology"] == "pooled"
    assert planner["sharing"] is None  # 'pooled' does not resurrect the legacy sharing block

    # The deprecated alias is accepted and resolved away before the runtime sees it,
    # while its legacy default sharing block is preserved.
    legacy = Preprocessor.compile(_spec(planner={"topology": "two-stage"}), domain=DOMAIN)
    legacy_planner = legacy.spec.assignment_settings["profile"]["planner"]
    assert legacy_planner["topology"] == "pooled"
    assert legacy_planner["sharing"] == {"type": "transfer-when-cheaper", "params": {}}
    # Recipe echoes the resolved name, so a recompile is a fixed point.
    assert legacy.recipe["planner"]["topology"] == "pooled"
    again = Preprocessor.compile(legacy.recipe, domain=DOMAIN)
    assert again.spec.assignment_settings["profile"]["planner"] == legacy_planner

    with pytest.raises(SpecValidationError, match="unknown topology"):
        Preprocessor.compile(_spec(planner={"topology": "swarm"}), domain=DOMAIN)
