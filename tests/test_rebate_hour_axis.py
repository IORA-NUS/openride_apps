"""R2-1 / review F2 — the rebate and order-demand curves are different clocks.

`sampling.py` never reads the epoch, so an order authored at hour ``H`` arrives at a
step whose WALL-CLOCK label is ``(H + reference_hour) % 24``. `price_at` does read the
true wall clock. At the historical ``08:00`` default the two axes in one ``spec.json``
are eight hours apart, which silently inverts what a schedule means — a "ports pay at
night" band lands on the authored demand peak.

Remediation is plan §18.3 option C: realign the EPOCH (labels only) rather than the
sampling arithmetic (which would move every order's arrival step in all 12
curve-carrying scenarios). These tests pin the measurement that makes C safe, the
hard-error that makes the collision unauthorable, and the stamping that makes existing
runs self-describing.
"""

import copy
import datetime
import json
import os
import tempfile

import pytest

from apps.container_logistics.datagen.preprocess import Preprocessor, SpecValidationError

DOMAIN = "container_logistics"

_REBATE = {
    "currency": "credit",
    "resolution": "hour",
    "points": [{"hour": 22, "amount": 40.0}, {"hour": 8, "amount": -25.0}],
}


def _spec(with_rebate=True, reference_time=None):
    spec = {
        "name": "Hour axis probe",
        "slug": "hour_axis_probe",
        "domain": DOMAIN,
        "simulationDays": 1,
        "seed": 4242,
        "agents": {
            "truck": {"count": 6},
            "order": {"count": 40},
            "facility": {"count": 12},
        },
        "orderDemandCurve": {
            "resolution": "hour",
            "points": [{"hour": h, "weight": 1.0 if 6 <= h <= 22 else 0.05}
                       for h in range(24)],
        },
        "overrides": {"facility": {}},
    }
    if with_rebate:
        spec["overrides"]["facility"]["rebate"] = copy.deepcopy(_REBATE)
    if reference_time:
        spec["referenceTime"] = reference_time
    return spec


def _compile(spec, reference_time="2020-01-01 00:00:00"):
    return Preprocessor.compile(copy.deepcopy(spec), domain=DOMAIN,
                                reference_time=reference_time)


_WORLD_CACHE = {}


def _world():
    """The `facilityRulesWorld` baseline for this module's facility world.

    `facilityRules` without a recorded fingerprint is refused at compile, so any test
    authoring rules needs this. Derived from a rule-less compile — exactly what
    `openride scenario rules-baseline` does — so it cannot drift from the generator.
    """
    if "w" not in _WORLD_CACHE:
        from apps.container_logistics.datagen import facility_rules as fr

        compiled = _compile(_spec(with_rebate=False))
        sites = compiled.spec.facility_settings["profile"]["facilities"]
        _WORLD_CACHE["w"] = fr.world_snapshot(sites, seed=None)
    return _WORLD_CACHE["w"]


# ------------------------------------------------- the measurement that justifies C


def test_reference_time_shift_does_not_change_generated_collections():
    """Option C is label-only. This is the claim the whole remediation rests on.

    If moving the epoch changed generation, realigning it would be a dynamics change
    (option B's problem) rather than a relabelling, and §18.3 would be wrong. Pinned so
    that a future change to ``sampling.py`` which DOES read the epoch cannot silently
    invalidate the argument.
    """
    from apps.container_logistics.datagen import ScenarioGenerator

    outs = {}
    for ref in ("2020-01-01 08:00:00", "2020-01-01 00:00:00"):
        compiled = _compile(_spec(with_rebate=False), reference_time=ref)
        result = ScenarioGenerator(compiled.spec, catalog=Preprocessor.catalog()).generate()
        outs[ref] = result

    a, b = outs["2020-01-01 08:00:00"], outs["2020-01-01 00:00:00"]
    J = lambda x: json.dumps(x, sort_keys=True, default=str)
    for role in ("truck", "order", "facility", "assignment", "analytics"):
        assert J(getattr(a, role)) == J(getattr(b, role)), (
            f"{role} collection changed when only the epoch moved — option C is not "
            "label-only and plan §18.3 is invalid"
        )
    # The specific quantity the argument needs: arrival STEPS are unchanged.
    steps_a = [v.get("request_time_step") for v in a.order.values()]
    steps_b = [v.get("request_time_step") for v in b.order.values()]
    assert steps_a == steps_b, "order arrival steps moved with the epoch"
    assert any(s is not None for s in steps_a), "fixture produced no arrival steps"


def test_sampling_still_does_not_read_the_epoch():
    """The structural fact option C depends on, asserted rather than assumed.

    If someone later makes ``sample_request_time_step`` epoch-aware, option C silently
    becomes a dynamics change and this test is the tripwire.
    """
    import inspect

    from apps.container_logistics.datagen import sampling

    src = inspect.getsource(sampling)
    for token in ("reference_time", "REFERENCE_TIME"):
        assert token not in src, (
            f"sampling.py now references {token!r}; plan §18.3's 'labels only' argument "
            "for option C no longer holds and the remediation must be re-derived"
        )


# ------------------------------------------------------------------- the hard error


def test_rebate_schedule_with_non_midnight_epoch_is_rejected():
    """Refused, not warned. Money gets no silent fallbacks."""
    with pytest.raises(SpecValidationError) as exc:
        _compile(_spec(with_rebate=True), reference_time="2020-01-01 08:00:00")
    msg = str(exc.value)
    assert "midnight" in msg
    assert "hour 8" in msg                  # names the offset
    assert "referenceTime" in msg           # names the one-line fix
    assert "2020-01-01 00:00:00" in msg


@pytest.mark.parametrize("ref", ["2020-01-01 04:00:00", "2020-01-01 08:00:00",
                                 "2020-01-01 23:00:00"])
def test_every_non_midnight_epoch_is_rejected_not_just_the_default(ref):
    with pytest.raises(SpecValidationError, match="midnight"):
        _compile(_spec(with_rebate=True), reference_time=ref)


def test_midnight_epoch_compiles_fine():
    compiled = _compile(_spec(with_rebate=True), reference_time="2020-01-01 00:00:00")
    assert compiled.orsim_settings["REFERENCE_TIME"] == "2020-01-01 00:00:00"


def test_a_spec_without_a_rebate_is_untouched_by_the_rule():
    """The rule must not break any of the 12 existing curve-carrying scenarios."""
    compiled = _compile(_spec(with_rebate=False), reference_time="2020-01-01 08:00:00")
    assert compiled.orsim_settings["REFERENCE_TIME"] == "2020-01-01 08:00:00"


def test_the_midnight_refusal_fires_for_a_rules_authored_schedule():
    """The collision is about carrying a schedule, not about which key carries it.

    Half the proof that the `facilityRules` migration did not silently disarm this
    guard. The refusal used to be scoped by the presence of `overrides.facility.rebate`
    / `.rebate_by_code`; the migration makes both absent, so a guard still keyed off
    them would stop firing with every test still green. It now judges the EFFECTIVE,
    post-resolution set (`site["rebate"]`), and this fixture authors the schedule
    ONLY through `facilityRules` — there is no scenario-wide rebate at all.
    """
    spec = _spec(with_rebate=False)
    assert "rebate" not in spec["overrides"]["facility"], (
        "fixture: the schedule must reach the sites via facilityRules alone"
    )
    spec["facilityRules"] = [
        {"match": {"code": "CT"}, "set": {"rebate": copy.deepcopy(_REBATE)}}
    ]
    spec["facilityRulesWorld"] = copy.deepcopy(_world())
    with pytest.raises(SpecValidationError, match="midnight"):
        _compile(spec, reference_time="2020-01-01 08:00:00")


# ------------------------------------------------- R3-5: the epoch itself is validated


@pytest.mark.parametrize("bad", [
    "not-a-timestamp",
    "2020-01-01T00:00:00+08:00",   # an offset: "which wall clock" is ambiguous
    "2020-01-01T00:00:00Z",
    "2020-01-01 00:00",            # a natural authoring the compiler used to bless
    "2020-01-01",
])
def test_unparseable_epoch_refused_at_compile(bad):
    """R3-5 / review R2-4 — the ``None`` exemption is GONE, for every spec.

    An unreadable epoch used to be waved through by ``ref_hour not in (0, None)``. That
    is the wrong default for provenance: the run then carries an epoch nothing can
    interpret, and the ISO-offset variant was actively stamped ``axes_aligned: true``.
    """
    with pytest.raises(SpecValidationError, match="canonical"):
        _compile(_spec(with_rebate=True), reference_time=bad)


def test_the_epoch_is_validated_even_without_a_rebate_schedule():
    """Unconditional. An epoch is not only a money concern."""
    with pytest.raises(SpecValidationError, match="canonical"):
        _compile(_spec(with_rebate=False), reference_time="2020-01-01T00:00:00Z")


def test_iso_offset_epoch_is_never_stamped_aligned():
    """The specific confident-but-wrong stamp R2-4 found.

    Before R3-4/R3-5 the compiler read ``'2020-01-01T00:00:00+08:00'`` as hour 0 and
    stamped the run ``axes_aligned: true``, while the runtime could not parse it at all
    and stamped ``reference_hour: None``. Two provenance records, same run, opposite
    verdicts. It is now refused outright — and the shared helper never reports 0 for it.
    """
    from apps.container_logistics.rebate import reference_hour

    assert reference_hour("2020-01-01T00:00:00+08:00") is None
    with pytest.raises(SpecValidationError):
        _compile(_spec(with_rebate=False), reference_time="2020-01-01T00:00:00+08:00")


# ---------------------------------------- R3-1: the control arm's epoch is protected


def test_a_null_save_does_not_wipe_a_declared_epoch():
    """R3-1 / review R2-3 — the control arm is the one that needed protecting.

    A dashboard payload that carries ``referenceTime`` with a ``null`` (what a generic
    form serialiser produces for an untouched field) used to stand the ``CARRY_IF_ABSENT``
    inheritance down by PRESENCE and wipe the declared epoch. The scenario then reverted
    to the 08:00 caller default — silently, and only on the arm nobody inspects. A paired
    estimator cannot see it, because each arm stays internally consistent while their
    demand curves sit on different wall clocks.

    Fixed at the layer where the loss happens (``_NULL_MEANS_UNSUPPLIED``) rather than by
    refusing at compile: protecting the value beats detecting its absence afterwards, and
    a compile-time refusal would reject every ordinary save, since ``assemble_spec``
    writes a null for any spec that never declared an epoch.
    """
    from apps.container_logistics.scenario.frontend_scenario_spec import assemble_spec

    previous = {"name": "arm", "slug": "arm", "domain": DOMAIN,
                "referenceTime": "2020-01-01 00:00:00"}
    # The save SPEAKS the key, with a null.
    out = assemble_spec(
        {"name": "arm", "slug": "arm", "domain": DOMAIN, "referenceTime": None},
        DOMAIN, previous=previous,
    )
    assert out["referenceTime"] == "2020-01-01 00:00:00", (
        "a null-bearing save wiped the declared epoch; the control arm would silently "
        "revert to the 08:00 default"
    )
    # An empty string is the same mistake in a different costume.
    out2 = assemble_spec(
        {"name": "arm", "slug": "arm", "domain": DOMAIN, "referenceTime": "   "},
        DOMAIN, previous=previous,
    )
    assert out2["referenceTime"] == "2020-01-01 00:00:00"


def test_a_real_epoch_in_the_body_still_overrides_the_previous_one():
    """The exception must not make the key un-editable."""
    from apps.container_logistics.scenario.frontend_scenario_spec import assemble_spec

    out = assemble_spec(
        {"name": "arm", "slug": "arm", "domain": DOMAIN,
         "referenceTime": "2020-01-01 08:00:00"},
        DOMAIN,
        previous={"name": "arm", "slug": "arm", "domain": DOMAIN,
                  "referenceTime": "2020-01-01 00:00:00"},
    )
    assert out["referenceTime"] == "2020-01-01 08:00:00"


def test_clearing_still_works_for_keys_whose_domain_contains_null():
    """Every key whose domain CONTAINS null keeps "presence, not truthiness", so
    clearing stays possible.

    **Renamed and corrected (F1).** This was
    `test_the_null_exception_is_scoped_to_the_epoch_key_only` and asserted
    `_NULL_MEANS_UNSUPPLIED == {"referenceTime"}` — i.e. it pinned the INSTANCE fix as
    complete. That premise is exactly what failed: an opt-in safety list leaves every
    newly registered key defaulting to the dangerous behaviour, and `facilityRules`
    was then registered outside it and would have been silently wiped by a null
    payload. The list is now DERIVED from a mandatory, import-checked per-key policy
    table, so asserting its membership pins nothing worth pinning.

    The behavioural claim below — an ordinary key can still be cleared — is the part
    that was always the point, and it is unchanged.
    """
    from apps.container_logistics.scenario.frontend_scenario_spec import (
        NullPolicy, SPEC_KEY_NULL_POLICY, assemble_spec,
    )

    assert SPEC_KEY_NULL_POLICY["referenceTime"] is NullPolicy.UNSUPPLIED
    assert SPEC_KEY_NULL_POLICY["solver"] is NullPolicy.CLEARS
    out = assemble_spec(
        {"name": "arm", "slug": "arm", "domain": DOMAIN, "solver": None},
        DOMAIN,
        previous={"name": "arm", "slug": "arm", "domain": DOMAIN,
                  "solver": "GreedyNearest"},
    )
    assert out["solver"] is None, "clearing an ordinary key must still work"


def test_a_declared_non_canonical_epoch_is_still_refused():
    """The format rule still applies to anything actually declared."""
    spec = _spec(with_rebate=False)
    spec["referenceTime"] = "2020-01-01T00:00:00Z"
    with pytest.raises(SpecValidationError, match="canonical"):
        _compile(spec, reference_time="2020-01-01 08:00:00")


def test_an_absent_reference_time_is_still_fine():
    """The 13 scenarios predating the key carry none at all and must keep compiling."""
    spec = _spec(with_rebate=False)
    assert "referenceTime" not in spec
    compiled = _compile(spec, reference_time="2020-01-01 08:00:00")
    assert compiled.orsim_settings["REFERENCE_TIME"] == "2020-01-01 08:00:00"


def test_control_and_treatment_compile_to_the_same_epoch():
    """The R2-3 regression check, and the precondition for every experiment in §19.3."""
    from apps.container_logistics.scenario.spec_compile import compile_spec_to_bundle

    epochs = {}
    for arm, with_rebate in (("treatment", True), ("control", False)):
        spec = _spec(with_rebate=with_rebate, reference_time="2020-01-01 00:00:00")
        with tempfile.TemporaryDirectory() as d:
            bundle = compile_spec_to_bundle(
                spec, domain=DOMAIN, scenario_dir=d, slug=f"arm_{arm}",
                name=f"arm {arm}", source="spec",
            )
        epochs[arm] = bundle["settings"]["REFERENCE_TIME"]

    assert epochs["control"] == epochs["treatment"] == "2020-01-01 00:00:00", (
        f"the two arms compiled to different epochs: {epochs} — the F2 confound rebuilt "
        "inside the comparison, where a paired estimator is blind to it"
    )


# ---------------------------------------------------------------------- the stamping


def test_hour_axis_is_stamped_for_both_curves():
    compiled = _compile(_spec(with_rebate=True), reference_time="2020-01-01 00:00:00")
    axis = compiled.recipe["hour_axis"]
    assert "REFERENCE_TIME" in axis["orderDemandCurve"]
    assert "wall clock" in axis["rebate"]
    assert axis["reference_hour"] == 0
    assert axis["demand_to_wall_offset_hours"] == 0
    assert axis["axes_aligned"] is True


def test_hour_axis_reports_a_misaligned_bundle_honestly():
    """A bundle compiled on the old axis must SAY it is misaligned, retroactively."""
    compiled = _compile(_spec(with_rebate=False), reference_time="2020-01-01 08:00:00")
    axis = compiled.recipe["hour_axis"]
    assert axis["reference_hour"] == 8
    assert axis["demand_to_wall_offset_hours"] == 8
    assert axis["axes_aligned"] is False


def test_reference_time_round_trips_into_the_recipe():
    """A recompile must keep the authored axis, not re-inherit the caller default."""
    compiled = _compile(_spec(with_rebate=True), reference_time="2020-01-01 00:00:00")
    assert compiled.recipe["referenceTime"] == "2020-01-01 00:00:00"


def test_declared_reference_time_beats_the_caller_default():
    """The whole point of registering the spec key (plan §18.3 step 3)."""
    from apps.container_logistics.scenario.spec_compile import compile_spec_to_bundle

    spec = _spec(with_rebate=True, reference_time="2020-01-01 00:00:00")
    with tempfile.TemporaryDirectory() as d:
        bundle = compile_spec_to_bundle(
            spec, domain=DOMAIN, scenario_dir=d, slug="hour_axis_probe",
            name="Hour axis probe", source="spec",
        )
    # The caller passed no reference_time, so the 08:00 default would otherwise win —
    # and with a rebate schedule present that would have RAISED.
    assert bundle["settings"]["REFERENCE_TIME"] == "2020-01-01 00:00:00"


def test_reference_time_is_a_registered_spec_key():
    """Unregistered, it would vanish on every dashboard save — the G23/F1 mechanism."""
    from apps.container_logistics.scenario.frontend_scenario_spec import (
        SPEC_KEYS, Carry, assemble_spec,
    )

    assert SPEC_KEYS["referenceTime"] is Carry.CARRY_IF_ABSENT
    previous = {"name": "x", "slug": "x", "domain": DOMAIN,
                "referenceTime": "2020-01-01 00:00:00"}
    out = assemble_spec({"name": "x", "slug": "x", "domain": DOMAIN}, DOMAIN,
                        previous=previous)
    assert out["referenceTime"] == "2020-01-01 00:00:00"


# ------------------------------------- R3-4: ONE hour rule, shared by both call sites

#: The reviewer's R2-5 table, used verbatim as the fixture. Before R3-4 the compiler and
#: the runtime disagreed on FOUR of these seven.
_EPOCH_CASES = [
    "2020-01-01 00:00:00",
    "2020-01-01 08:00:00",
    "2020-01-01T00:00:00+08:00",
    "2020-01-01T00:00:00Z",
    "2020-01-01 00:00",
    "2020-01-01",
    "not a date",
    # An 8th case, added deliberately: the reviewer's seven no longer DISCRIMINATE once
    # R3-5 made the compiler side strict — both would agree on all seven even with two
    # separate parsers, so the table alone would silently stop guarding R3-4. This one
    # is parsed as hour 8 by the old "%Y-%m-%dT%H:%M:%S" derivation and as None by the
    # single canonical rule, so it fails the moment a second parser reappears.
    "2020-01-01T08:00:00",
]


def test_both_hour_axis_stamps_agree_on_seven_epoch_formats():
    """``recipe.hour_axis`` and ``meta.rebate.hour_axis`` describe the SAME run.

    Two independent derivations meant one artefact could say ``axes_aligned: true`` while
    the other said ``false``. This is the provenance analogue of ``price_at``'s "one
    rule, one function" discipline.
    """
    from apps.container_logistics.datagen.preprocess import Preprocessor
    from apps.container_logistics.scenario.scenario_manager import ScenarioManager

    class _Fake:
        pass

    disagreements = []
    for epoch in _EPOCH_CASES:
        compiler_hour = Preprocessor._reference_hour(epoch)
        fake = _Fake()
        fake.orsim_settings = {"REFERENCE_TIME": epoch}
        runtime = ScenarioManager._hour_axis_stamp(fake)
        if compiler_hour != runtime["reference_hour"]:
            disagreements.append((epoch, compiler_hour, runtime["reference_hour"]))
        # ...and the derived verdict must follow the same rule, not be computed twice.
        assert runtime["axes_aligned"] is (runtime["reference_hour"] == 0)

    assert disagreements == [], (
        "compiler and runtime disagree on the epoch hour for: " + repr(disagreements)
    )


def test_the_shared_helper_never_reports_midnight_for_an_unreadable_epoch():
    """``None`` means UNKNOWN, never midnight — the distinction R2-4 turned on."""
    from apps.container_logistics.rebate import reference_hour

    for epoch in _EPOCH_CASES[2:]:
        assert reference_hour(epoch) is None, f"{epoch!r} was resolved to an hour"
    assert reference_hour("2020-01-01 00:00:00") == 0
    assert reference_hour(datetime.datetime(2020, 1, 1, 13, 0)) == 13


def test_there_is_only_one_epoch_parser_left():
    """A second `strptime` on an epoch is how the two derivations drifted apart."""
    import inspect

    from apps.container_logistics.scenario import scenario_manager

    src = inspect.getsource(scenario_manager.ScenarioManager._hour_axis_stamp)
    assert "strptime" not in src, (
        "scenario_manager re-derives the epoch hour again; it must delegate to "
        "rebate.reference_hour or the two provenance stamps can drift"
    )
    assert "reference_hour" in src


# ------------------------------------------------------- R3-9: a policy flag is a bool


@pytest.mark.parametrize("bad", ["false", "true", "no", 1, 0, None, [], {}])
def test_string_false_does_not_enable_the_seam(bad):
    """R3-9 / review R2-8. ``bool("false")`` is True.

    A JSON-ish client sending the *string* ``"false"`` would have switched the opt-in
    solver seam ON. A boolean policy flag gets no truthiness coercion — the same
    discipline ``rebate.py::_is_real_number`` applies to money.
    """
    from apps.container_logistics.datagen.preprocess import Preprocessor

    with pytest.raises(SpecValidationError, match="boolean"):
        Preprocessor._normalize_planner({"topology": "pooled", "rebate_aware": bad})


def test_real_booleans_are_accepted_and_absent_defaults_false():
    from apps.container_logistics.datagen.preprocess import Preprocessor

    assert Preprocessor._normalize_planner({})["rebate_aware"] is False
    assert Preprocessor._normalize_planner({"rebate_aware": True})["rebate_aware"] is True
    assert Preprocessor._normalize_planner({"rebate_aware": False})["rebate_aware"] is False


# --------------------------------------------- R3-15: an ignored path is now an error


def test_conflicting_datahub_dir_raises_instead_of_being_ignored():
    """R3-15 / review R2-15. The anchoring is deliberate; the silent discard was not.

    A caller passing a temp directory expecting isolation received the REAL
    ``scenarios/`` tree, which is how a probe wrote into the source tree.
    """
    import os

    from apps.container_logistics.scenario.frontend_scenario_spec import scenario_root

    anchored = scenario_root()
    repo_root = os.path.dirname(anchored)

    # The repo root itself.
    assert scenario_root(repo_root) == anchored
    # **The real production caller**, and the case that matters most here: the control
    # plane passes `<repo>/datahub`, which does NOT contain `<repo>/scenarios`. A
    # containment rule rejects it — and did, breaking `list-scenarios` and a 500-truck
    # verification run, while the whole test suite stayed green because every test used
    # a synthetic path. This assertion is the one that would have caught it.
    assert scenario_root(os.path.join(repo_root, "datahub")) == anchored
    assert scenario_root(os.path.join(repo_root, "datahub", "container_logistics")) == anchored

    # An unrelated path outside the checkout would have been silently discarded — this
    # is how a probe expecting isolation wrote into the real source tree.
    with pytest.raises(ValueError, match="outside this checkout"):
        scenario_root("/tmp/a_probe_that_expected_isolation")


def test_the_control_plane_scenario_listing_still_works():
    """End-to-end guard for the same break, at the layer that actually failed.

    `list_scenarios` is what `openride_control.command list-scenarios` calls, with the
    production `datahub_dir`. A unit test on `scenario_root` alone did not catch the
    regression; this does.
    """
    import os

    from apps.container_logistics.scenario.frontend_scenario_spec import (
        list_scenarios, scenario_root,
    )

    repo_root = os.path.dirname(scenario_root())
    result = list_scenarios(os.path.join(repo_root, "datahub"), "container_logistics")
    assert isinstance(result, list)


def test_the_parameter_is_kept_so_call_sites_still_work():
    """Removing it would break every caller; the defect was the silent discard."""
    import inspect

    from apps.container_logistics.scenario.frontend_scenario_spec import scenario_root

    params = inspect.signature(scenario_root).parameters
    assert "datahub_dir" in params and "domain" in params
    assert scenario_root("") == scenario_root()
