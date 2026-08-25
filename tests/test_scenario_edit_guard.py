from pathlib import Path
"""FIX-6 layer 2 on the CLI ``edit_scenario`` path (plan §13.4, review finding F2).

``generate_scenario`` got the destructive-save guard; ``edit_scenario`` did not. It was
plausible that it did not need one — ``edit_scenario`` is the *merging* path, and a merge
that preserves what the caller omits cannot lose a field it never heard of.

**That reasoning is wrong, and these tests are built on the probe that disproved it.** The
merge is ``{**base, **patch}`` — shallow, top-level. A patch that mentions ``cooperation``
at all replaces the whole block; it does not deepen into it. Driving the real functions on
a real folder, before the guard existed:

* patch ``{"cooperation": {"structures": [{"id": "consortium", "edges": []}]}}`` turned a
  compiled 2-pool / 4-edge consortium into ``edges: [] pools: []`` — the structure survived
  by name and stayed ``active``;
* patch ``{"cooperation": {"structures": []}}`` deleted the structure outright and the
  compiled bundle fell back to ``active: "no-coop"``;
* patch ``{"simulationDays": 2}`` (no ``cooperation`` key) preserved everything — that is
  the one case the merge really does protect, and the guard must stay silent for it.

So the guard is required here, and it must be evaluated on the POST-merge block: keying it
off ``patch`` alone would fire on every unrelated edit.

Everything below drives the REAL ``edit_scenario`` / ``cmd_edit_scenario`` against a real
temporary scenario folder and asserts on the COMPILED bundle — the assignment profile the
runtime actually reads — not on an intermediate dict. Nothing under test is stubbed.
"""

import json
import os
import shutil
import sys
import tempfile

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # openride_control now lives IN the repo

from apps.container_logistics.scenario.frontend_scenario_spec import (  # noqa: E402
    CooperationResetRequired,
    edit_scenario,
    generate_scenario,
    read_spec,
    scenario_dir,
)
from apps.container_logistics.scenario.scenario_bundle import load_bundle  # noqa: E402

DOMAIN = "container-logistics-sim-test"

HAULIERS = [
    {"id": "acme", "name": "Acme", "fleet_share": 25, "order_share": 25},
    {"id": "borax", "name": "Borax", "fleet_share": 25, "order_share": 25},
    {"id": "cargo", "name": "Cargo", "fleet_share": 25, "order_share": 25},
    {"id": "delta", "name": "Delta", "fleet_share": 25, "order_share": 25},
]

# Overlapping pools (plan §3.2). Not expressible as independent pairs, which is why
# the derived-edge projection is lossy.
CONSORTIUM_POOLS = [
    {"id": "north", "members": ["acme", "borax", "cargo"]},
    {"id": "south", "members": ["cargo", "delta"]},
]
CONSORTIUM = {
    "active": "consortium",
    "structures": [{"id": "consortium", "pools": CONSORTIUM_POOLS}],
}
CONSORTIUM_EDGES = [
    ["acme", "borax"],
    ["acme", "cargo"],
    ["borax", "cargo"],
    ["cargo", "delta"],
]
EDGES_ONLY = {
    "active": "pair",
    "structures": [{"id": "pair", "edges": [["acme", "borax"]]}],
}


@pytest.fixture
def datahub_tmp():
    tmp = tempfile.mkdtemp(prefix="edit_guard_")
    prev = os.environ.get("ORSIM_SCENARIOS_DIR")
    os.environ["ORSIM_SCENARIOS_DIR"] = os.path.join(tmp, DOMAIN, "scenarios")
    yield tmp
    if prev is None:
        os.environ.pop("ORSIM_SCENARIOS_DIR", None)
    else:
        os.environ["ORSIM_SCENARIOS_DIR"] = prev
    shutil.rmtree(tmp, ignore_errors=True)


def _create(datahub: str, slug: str, cooperation) -> None:
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


def _compiled_cooperation(datahub: str, slug: str) -> dict:
    """The cooperation block on the compiled assignment profile — runtime truth."""
    collections, _settings, _recipe = load_bundle(scenario_dir(datahub, DOMAIN, slug))
    agent = next(iter(collections["assignment"].values()))
    return agent["profile"]["cooperation"]


def _structure(cooperation: dict, sid: str) -> dict:
    return next(s for s in cooperation["structures"] if s["id"] == sid)


# --- the merge genuinely CAN empty cooperation --------------------------------


def test_a_patch_that_replaces_cooperation_is_refused(datahub_tmp):
    """The headline hole: a shallow merge replaces the whole block, it does not deepen."""
    _create(datahub_tmp, "empty-edges", CONSORTIUM)
    before = json.dumps(_compiled_cooperation(datahub_tmp, "empty-edges"), sort_keys=True)

    with pytest.raises(CooperationResetRequired) as exc:
        edit_scenario(
            datahub_tmp,
            DOMAIN,
            "empty-edges",
            {"cooperation": {"active": "consortium", "structures": [{"id": "consortium", "edges": []}]}},
        )
    assert "consortium" in str(exc.value)
    assert isinstance(exc.value, ValueError)  # back-compat for existing handlers

    # Refused BEFORE anything destructive: the folder is untouched, not half-written.
    assert json.dumps(_compiled_cooperation(datahub_tmp, "empty-edges"), sort_keys=True) == before


def test_a_patch_that_drops_the_structure_is_refused(datahub_tmp):
    """Deletion is emptying too — the structure disappears and 'active' silently
    falls back to no-coop, which is the worst version of F2: a run that reports
    cooperation as configured while cooperating with nobody."""
    _create(datahub_tmp, "drop-struct", CONSORTIUM)

    with pytest.raises(CooperationResetRequired):
        edit_scenario(datahub_tmp, DOMAIN, "drop-struct", {"cooperation": {"structures": []}})

    coop = _compiled_cooperation(datahub_tmp, "drop-struct")
    assert coop["active"] == "consortium"
    assert _structure(coop, "consortium")["pools"] == CONSORTIUM_POOLS


def test_an_edges_authored_structure_is_guarded_too(datahub_tmp):
    """The guard is about the CLASS (any cooperation content), not about pools."""
    _create(datahub_tmp, "edges-guard", EDGES_ONLY)

    with pytest.raises(CooperationResetRequired) as exc:
        edit_scenario(
            datahub_tmp,
            DOMAIN,
            "edges-guard",
            {"cooperation": {"active": "pair", "structures": [{"id": "pair", "edges": []}]}},
        )
    assert "pair" in str(exc.value)
    assert _structure(_compiled_cooperation(datahub_tmp, "edges-guard"), "pair")["edges"] == [
        ["acme", "borax"]
    ]


# --- and it must NOT fire on the cases the merge really does protect ----------


def test_a_patch_that_omits_cooperation_still_succeeds(datahub_tmp):
    """The regression that would make the guard useless in practice: almost every
    real ``--set`` edit never mentions cooperation, and must not be refused."""
    _create(datahub_tmp, "omit-coop", CONSORTIUM)

    edit_scenario(datahub_tmp, DOMAIN, "omit-coop", {"simulationDays": 2})

    coop = _compiled_cooperation(datahub_tmp, "omit-coop")
    assert _structure(coop, "consortium")["pools"] == CONSORTIUM_POOLS
    assert _structure(coop, "consortium")["edges"] == CONSORTIUM_EDGES
    assert read_spec(scenario_dir(datahub_tmp, DOMAIN, "omit-coop"))["simulationDays"] == 2


def test_a_scenario_with_no_cooperation_can_be_edited_freely(datahub_tmp):
    """The always-present no-coop baseline has no content, so it can never trip
    the guard — otherwise every plain scenario becomes uneditable."""
    _create(datahub_tmp, "no-coop-only", None)

    edit_scenario(datahub_tmp, DOMAIN, "no-coop-only", {"cooperation": {"structures": []}})
    edit_scenario(datahub_tmp, DOMAIN, "no-coop-only", {"simulationDays": 3})

    assert read_spec(scenario_dir(datahub_tmp, DOMAIN, "no-coop-only"))["simulationDays"] == 3


def test_editing_cooperation_content_is_allowed(datahub_tmp):
    """Changing cooperation is not emptying it — the guard must not freeze the field."""
    _create(datahub_tmp, "swap-edges", EDGES_ONLY)

    edit_scenario(
        datahub_tmp,
        DOMAIN,
        "swap-edges",
        {
            "cooperation": {
                "active": "pair",
                "structures": [{"id": "pair", "edges": [["borax", "cargo"]]}],
            }
        },
    )

    assert _structure(_compiled_cooperation(datahub_tmp, "swap-edges"), "pair")["edges"] == [
        ["borax", "cargo"]
    ]


# --- the escape hatch has to actually work ------------------------------------


def test_the_reset_flag_lets_an_edges_authored_reset_through(datahub_tmp):
    _create(datahub_tmp, "reset-edges", EDGES_ONLY)

    edit_scenario(
        datahub_tmp,
        DOMAIN,
        "reset-edges",
        {"cooperation": {"active": "pair", "structures": [{"id": "pair", "edges": []}]}},
        allow_cooperation_reset=True,
    )

    assert _structure(_compiled_cooperation(datahub_tmp, "reset-edges"), "pair")["edges"] == []


def test_the_reset_flag_lets_a_POOLS_authored_reset_through(datahub_tmp):
    """The case the hatch exists for, and the one that was dead.

    Probed on the pre-existing ``generate_scenario``: passing the flag cleared the
    guard, then the layer-1 pools carry re-attached ``pools`` onto a body whose
    ``edges`` were now empty, and the Preprocessor rejected the save with
    ``SpecValidationError: 'pools' and 'edges' disagree``. An escape hatch that
    raises a different error instead is not an escape hatch. An explicit reset now
    stands the carry down.
    """
    _create(datahub_tmp, "reset-pools", CONSORTIUM)

    edit_scenario(
        datahub_tmp,
        DOMAIN,
        "reset-pools",
        {"cooperation": {"active": "consortium", "structures": [{"id": "consortium", "edges": []}]}},
        allow_cooperation_reset=True,
    )

    consortium = _structure(_compiled_cooperation(datahub_tmp, "reset-pools"), "consortium")
    assert consortium["edges"] == []
    assert consortium["pools"] == []


def test_generate_scenario_reset_flag_clears_a_pools_authored_structure(datahub_tmp):
    """Same hole on the generate path, fixed by the same one-line stand-down."""
    _create(datahub_tmp, "gen-reset-pools", CONSORTIUM)

    generate_scenario(
        datahub_tmp,
        DOMAIN,
        {
            "name": "gen-reset-pools",
            "slug": "gen-reset-pools",
            "simulationDays": 1,
            "orderCountUnit": "total",
            "agents": {
                "truck": {"count": 4},
                "order": {"count": 6},
                "facility": {"count": 3},
            },
            "earlyOrderCount": 0,
            "hauliers": [dict(h) for h in HAULIERS],
            "cooperation": {
                "active": "consortium",
                "structures": [{"id": "consortium", "edges": []}],
            },
        },
        overwrite=True,
        allow_cooperation_reset=True,
    )

    consortium = _structure(_compiled_cooperation(datahub_tmp, "gen-reset-pools"), "consortium")
    assert consortium["edges"] == []
    assert consortium["pools"] == []


# --- layer 1 on the edit path: a lossy client must not reshape the market -----


def test_an_edges_only_patch_preserves_authored_pools(datahub_tmp):
    """``edit_scenario`` did not pass ``previous`` to ``assemble_spec``, so the
    pools carry never ran here.

    Probed before the fix: patching the consortium with exactly its own DERIVED
    edges (all a pools-unaware client can send) recompiled the two overlapping
    pools into four 2-member pools ``p:acme+borax``… — a different market, and the
    guard cannot catch it because the structure still has content. That is F2's
    real payload, not merely the empty case.
    """
    _create(datahub_tmp, "edges-patch", CONSORTIUM)

    edit_scenario(
        datahub_tmp,
        DOMAIN,
        "edges-patch",
        {
            "cooperation": {
                "active": "consortium",
                "structures": [{"id": "consortium", "edges": CONSORTIUM_EDGES}],
            }
        },
    )

    consortium = _structure(_compiled_cooperation(datahub_tmp, "edges-patch"), "consortium")
    assert consortium["pools"] == CONSORTIUM_POOLS, (
        "authored pools were reshaped into pair-pools — the market silently changed"
    )
    assert consortium["edges"] == CONSORTIUM_EDGES


def test_an_edges_authored_scenario_gains_no_pools_key_on_edit(datahub_tmp):
    """The carry must be limited to authors of pools — an edges-only recipe must
    round-trip through edit unchanged."""
    _create(datahub_tmp, "edges-untouched", EDGES_ONLY)
    target = scenario_dir(datahub_tmp, DOMAIN, "edges-untouched")
    before = json.dumps(read_spec(target)["cooperation"], sort_keys=True)

    edit_scenario(datahub_tmp, DOMAIN, "edges-untouched", {"simulationDays": 2})

    after = read_spec(target)["cooperation"]
    assert json.dumps(after, sort_keys=True) == before
    assert all("pools" not in s for s in after["structures"])


# --- control-plane contract: a machine-readable code, never a message match ---


def test_control_plane_maps_the_edit_guard_to_the_distinct_code(datahub_tmp, monkeypatch):
    """Drives the REAL ``cmd_edit_scenario`` against the real folder — only the
    datahub location is redirected, never the function under test."""
    from openride_control import scenario as control_scenario

    _create(datahub_tmp, "cp-guard", CONSORTIUM)
    monkeypatch.setattr(control_scenario, "get_datahub_dir", lambda: datahub_tmp)

    payload = control_scenario.cmd_edit_scenario(
        "cp-guard",
        {"cooperation": {"active": "consortium", "structures": [{"id": "consortium", "edges": []}]}},
        domain=DOMAIN,
    )

    assert payload["ok"] is False
    assert payload["code"] == "COOP_RESET_REQUIRED", (
        "CooperationResetRequired subclasses ValueError, so an arm ordered after the "
        f"generic ValueError arm silently degrades it to INVALID; got {payload.get('code')!r}"
    )


def test_control_plane_edit_forwards_the_reset_flag(datahub_tmp, monkeypatch):
    from openride_control import scenario as control_scenario

    _create(datahub_tmp, "cp-reset", CONSORTIUM)
    monkeypatch.setattr(control_scenario, "get_datahub_dir", lambda: datahub_tmp)

    payload = control_scenario.cmd_edit_scenario(
        "cp-reset",
        {"cooperation": {"active": "consortium", "structures": [{"id": "consortium", "edges": []}]}},
        domain=DOMAIN,
        allow_cooperation_reset=True,
    )

    assert payload["ok"] is True, payload.get("message")
    assert _structure(_compiled_cooperation(datahub_tmp, "cp-reset"), "consortium")["edges"] == []


@pytest.mark.parametrize(
    "argv_extra, expected",
    [([], False), (["--allow-cooperation-reset"], True)],
)
def test_cli_threads_the_reset_flag_into_the_control_plane(argv_extra, expected, monkeypatch):
    """Drives the REAL argv dispatch in ``openride_control.command._run``.

    ``get_datahub_dir()`` is hardcoded to the repo's own ``datahub/``, so this cannot
    be pointed at a tmpdir from outside — running the CLI for real would edit a real
    scenario. Only the leaf ``cmd_edit_scenario`` is captured; the parser definition
    and the dispatch arm, which are what this asserts on, are the real ones. Without
    the parser argument the dispatch raises AttributeError rather than passing False.
    """
    import io

    from openride_control import command as control_command
    from openride_control import scenario as control_scenario

    seen: dict = {}

    def _capture(slug, patch, **kwargs):
        seen["slug"] = slug
        seen["patch"] = patch
        seen.update(kwargs)
        return {"ok": True, "action": "edit_scenario"}

    monkeypatch.setattr(control_scenario, "cmd_edit_scenario", _capture)
    monkeypatch.setattr("sys.stdin", io.StringIO('{"simulationDays": 2}'))

    rc = control_command._run(["edit-scenario", "--slug", "demo", *argv_extra])

    assert rc == 0
    assert seen["slug"] == "demo"
    assert seen["patch"] == {"simulationDays": 2}
    assert seen.get("allow_cooperation_reset") is expected


def test_edit_scenario_accepts_the_reset_keyword():
    """Signature guard: the control plane passes this keyword, so it must exist."""
    import inspect

    params = inspect.signature(edit_scenario).parameters
    assert "allow_cooperation_reset" in params
    assert params["allow_cooperation_reset"].default is False
