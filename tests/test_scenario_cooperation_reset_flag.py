from pathlib import Path
"""The destructive-save guard must be RECOVERABLE (plan §13.4 FIX-6 layer 2).

The guard refuses a save that would empty a structure's cooperation content, which
is right: an editor that does not understand a field must not silently delete it
(review F2). But §13.4 specifies an explicit ``allow_cooperation_reset`` escape
hatch, and an escape hatch nothing can reach is not implemented — it just converts a
data-loss bug into "the user can never clear cooperation again".

These tests pin the machine-readable path that makes the hatch reachable end to end:
guard -> distinct exception -> distinct control-plane code -> UI retry.
"""

import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # openride_control now lives IN the repo

from apps.container_logistics.scenario.frontend_scenario_spec import (  # noqa: E402
    CooperationResetRequired,
    _guard_cooperation_not_emptied,
)

_PREVIOUS = {
    "cooperation": {
        "active": "pair",
        "structures": [
            {"id": "no-coop", "edges": []},
            {"id": "pair", "edges": [["acme", "borax"]]},
        ],
    }
}


def test_guard_raises_the_distinct_type_not_a_bare_value_error():
    incoming = {"active": "pair", "structures": [{"id": "pair", "edges": []}]}
    with pytest.raises(CooperationResetRequired) as exc:
        _guard_cooperation_not_emptied(_PREVIOUS, incoming, "demo", allow_reset=False)
    assert "pair" in str(exc.value)
    # Back-compat: every existing `except ValueError` path must still catch it.
    assert isinstance(exc.value, ValueError)


def test_guard_is_silent_when_content_is_preserved():
    incoming = {"active": "pair", "structures": [{"id": "pair", "edges": [["acme", "borax"]]}]}
    _guard_cooperation_not_emptied(_PREVIOUS, incoming, "demo", allow_reset=False)  # must not raise


def test_the_allow_reset_flag_actually_bypasses_the_guard():
    incoming = {"active": "pair", "structures": [{"id": "pair", "edges": []}]}
    _guard_cooperation_not_emptied(_PREVIOUS, incoming, "demo", allow_reset=True)


def test_guard_is_silent_for_a_brand_new_scenario():
    _guard_cooperation_not_emptied(None, None, "demo", allow_reset=False)  # nothing to lose


def test_control_plane_maps_the_guard_to_a_machine_readable_code():
    """Without a distinct code the UI would have to string-match the message —
    exactly the fragility this project keeps getting burned by (CLAUDE.md §6.15)."""
    from openride_control import scenario as control_scenario

    original = control_scenario.generate_scenario

    def _raise(*_a, **_kw):
        raise CooperationResetRequired("would empty structure 'pair'")

    control_scenario.generate_scenario = _raise
    try:
        payload = control_scenario.cmd_generate_scenario({"domain": "container_logistics"})
    finally:
        control_scenario.generate_scenario = original

    assert payload["ok"] is False
    assert payload["code"] == "COOP_RESET_REQUIRED", (
        f"expected the distinct code so the UI can offer a retry, got {payload.get('code')!r}"
    )


def test_control_plane_forwards_the_reset_flag():
    """The flag must actually reach ``generate_scenario`` — otherwise the CLI arg
    and the API route are decorative."""
    from openride_control import scenario as control_scenario

    seen = {}
    original = control_scenario.generate_scenario

    def _capture(_datahub, _domain, _spec, **kwargs):
        seen.update(kwargs)
        return {"slug": "demo"}

    control_scenario.generate_scenario = _capture
    try:
        control_scenario.cmd_generate_scenario(
            {"domain": "container_logistics"}, allow_cooperation_reset=True
        )
    finally:
        control_scenario.generate_scenario = original

    assert seen.get("allow_cooperation_reset") is True


def test_generate_scenario_accepts_the_reset_keyword():
    """Signature guard: the control plane passes this keyword, so it must exist."""
    import inspect

    from apps.container_logistics.scenario.frontend_scenario_spec import generate_scenario

    params = inspect.signature(generate_scenario).parameters
    assert "allow_cooperation_reset" in params
    assert params["allow_cooperation_reset"].default is False
