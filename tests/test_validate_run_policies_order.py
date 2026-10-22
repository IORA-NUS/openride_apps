"""``validate-run-policies`` checks every NAME before it looks for the bundle.

What is locked (``docs/plans/policy_slots_round2_whole_tick.md`` §5 C4, S19): the
control plane's dry run calls ``scenario_overrides.precheck_override_names`` (every
named slot, policy and component registered; the params of every NAMED type valid)
BEFORE it loads the scenario, so a typo is reported as itself even for a scenario that
does not exist; params whose type is not named (``planner.inner.k=v``) are left for
the dry run against the bundle, which alone knows what they belong to. The payload
shape is ``{ok: true, action: "validate_run_policies", overrides}`` /
``{ok: false, action, message}``; ``overrides`` never carries ``--solver``.

Why: "scenario 'x' not found" for a misspelt solver sent users to fix the wrong thing.
"""

from __future__ import annotations

import json

import pytest

from openride_control import command
from openride_control.policies import validate_run_policy_args


def _payload(argv, capsys):
    rc = command._run(argv)
    return rc, json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def test_bad_solver_reported_even_without_a_bundle(capsys):
    """Verified to have teeth: moving ``precheck_override_names`` after
    ``load_assignment_collection`` in ``validate_run_policy_args`` reported
    "scenario 'nope' not found" instead (all three cases)."""
    rc, payload = _payload(["validate-run-policies", "--scenario", "nope", "--solver", "Nope"], capsys)
    assert rc == 1 and payload["ok"] is False and payload["action"] == "validate_run_policies"
    assert payload["message"].startswith("--solver 'Nope': Unknown planner policy 'Nope'"), payload
    msg = validate_run_policy_args("nope", "PooledMarket", [], [])
    assert msg.startswith("--solver 'PooledMarket' names a decomposition"), msg
    msg = validate_run_policy_args("nope", None, ["planner=GreedyNeerest"], [])
    assert "Unknown planner policy 'GreedyNeerest'" in msg, msg
    # A good name with no flags-only error reaches the bundle lookup.
    msg = validate_run_policy_args("nope", "GreedyNearest", [], [])
    assert msg.startswith("scenario 'nope' not found"), msg


def test_bad_nested_ref_reported_before_bundle_lookup(capsys):
    """Verified to have teeth: making ``check_named_overrides`` skip refs (only the top
    type checked) reported the missing scenario for the inner/offer typos; validating
    an UNNAMED ref's params against its default type refused the inner bonus."""
    for flags, needle in [
        (["--policy", "planner=PooledMarket", "--policy-param", "planner.inner=Nope"],
         "policy overrides.planner.params.inner: unknown planner policy 'Nope'"),
        (["--policy", "planner=PooledMarket", "--policy-param", "planner.offer=OfferAl"],
         "policy overrides.planner.params.offer: unknown market.offer component 'OfferAl'"),
        (["--policy", "planner=Partitioned", "--policy-param", "planner.inner=PooledMarket"],
         "'PooledMarket' decomposes the tick itself"),
        (["--policy", "planner=PooledMarket", "--policy-param", "planner.max_rounds=0"], "must be >= 1"),
        (["--policy", "planner=PooledMarket", "--policy-param", "planner.inner=GreedyNearest",
          "--policy-param", "planner.inner.dual_cycle_bonus_km=-1"], "must be >= 0"),
        (["--policy-param", "dispach.inner=X"], "no known slot is a prefix"),
    ]:
        rc, payload = _payload(["validate-run-policies", "--scenario", "nope", *flags], capsys)
        assert rc == 1 and needle in payload["message"], (flags, payload)
    # Unnamed ref params belong to the bundle's selection: only the bundle lookup can judge them.
    msg = validate_run_policy_args("nope", None, ["planner=PooledMarket"],
                                   ["planner.inner.dual_cycle_bonus_km=0"])
    assert msg.startswith("scenario 'nope' not found"), msg


def test_payload_ships_overrides_without_the_solver(tmp_path, monkeypatch, capsys):
    """The ok payload's ``overrides`` is ORSIM_POLICY_OVERRIDES (``--solver`` ships as
    ORSIM_SOLVER). Verified to have teeth: returning ``run_dispatch_overrides``' merged
    overrides (which carry the solver as ``planner.inner.type``) from
    ``validate_run_policy_args`` failed the equality."""
    from pathlib import Path

    from apps.container_logistics.scenario.spec_compile import compile_spec_to_bundle

    root = tmp_path / "root"
    monkeypatch.setenv("ORSIM_SCENARIOS_DIR", str(root))
    spec = json.loads((Path(__file__).resolve().parents[1] / "scenarios" / "collab_smoke_12t"
                       / "spec.json").read_text())
    (root / spec["slug"]).mkdir(parents=True)
    compile_spec_to_bundle(spec, domain="container-logistics-sim", scenario_dir=str(root / spec["slug"]),
                           slug=spec["slug"])
    rc, payload = _payload(["validate-run-policies", "--scenario", spec["slug"], "--solver", "RandomAssignment",
                            "--policy", "planner=PooledMarket",
                            "--policy-param", "planner.inner.candidates=spatial"], capsys)
    assert rc == 0, payload
    assert set(payload) == {"ok", "action", "overrides", "commandId"}
    assert payload["ok"] is True and payload["action"] == "validate_run_policies"
    assert payload["overrides"] == {"planner": {"type": "PooledMarket",
                                                 "params": {"inner": {"params": {"candidates": "spatial"}}}}}
