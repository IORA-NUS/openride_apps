"""Control-plane policy commands: run-flag validation, ``policy-validate``, ``policy-bench``.

Single responsibility: the control plane's policy surface.

- Turn ``run-simulation``'s ``--solver`` / ``--policy`` / ``--policy-param`` /
  ``--policy-overrides`` flags into
  the ``ORSIM_POLICY_OVERRIDES`` object and dry-run it against the scenario's compiled
  assignment profile, so a typo or an out-of-range param is refused synchronously
  instead of in the background sim's log (plan ``docs/plans/policy_slots_p0_p3.md``
  §4 P2.6). The dry run uses the same ``apply_policy_overrides`` the sim main
  applies, on a deep copy, and never writes. ``--capture-dispatch`` is checked with
  the sim's own parser (``dispatch.capture.parse_capture_spec``).
- ``policy-validate`` / ``policy-bench`` payloads (plan §4 P3.6) over the contract
  battery and the single-tick bench (``docs/policy_authoring.md``).

Runs in the control subprocess (not an agent): no celery restart applies.
"""

from __future__ import annotations

import copy
import json
import logging
import os
from typing import Any, Sequence

logger = logging.getLogger(__name__)

__all__ = [
    "build_run_policy_overrides",
    "parse_policy_overrides_json",
    "load_assignment_collection",
    "validate_run_policy_args",
    "cmd_validate_run_policies",
    "validate_capture_arg",
    "parse_bench_params",
    "cmd_policy_validate",
    "cmd_policy_bench",
]


def parse_policy_overrides_json(text: str | None) -> dict[str, Any]:
    """``--policy-overrides JSON``: a full ``{slot: {type?, timing?, params?}}`` object.

    ``{}`` when unset/blank. Shape only (an object of objects); slot names, entry keys,
    policy names and params are validated by the merge, so an unknown entry key is
    refused there, never stripped here. Raises ``PolicyError``.
    """
    from apps.container_logistics.policies.framework import PolicyError

    if text is None or not str(text).strip():
        return {}
    try:
        value = json.loads(text)
    except ValueError as exc:
        raise PolicyError(f"--policy-overrides is not valid JSON ({exc}): {text!r}") from exc
    if not isinstance(value, dict):
        raise PolicyError(
            f"--policy-overrides must be a JSON object of slot -> {{type?, timing?, params?}} (got {text!r})."
        )
    for slot, entry in value.items():
        if not isinstance(entry, dict):
            raise PolicyError(
                f"--policy-overrides.{slot} must be an object {{type?, timing?, params?}} (got {entry!r})."
            )
    return value


def _set_once(target: dict[str, Any], key: str, value: Any, field: str, origin: str) -> None:
    """``target[key] = value``; raise ``PolicyError`` if ``--policy-overrides`` set it differently."""
    from apps.container_logistics.policies.framework import PolicyError

    # A bool never equals a number here (``True == 1`` in Python; plan §2.6).
    if key in target and (target[key] != value or isinstance(target[key], bool) != isinstance(value, bool)):
        raise PolicyError(
            f"{origin} sets {field}={value!r} but --policy-overrides sets it to "
            f"{target[key]!r}; pass each field once."
        )
    target[key] = value


def build_run_policy_overrides(
    solver: str | None,
    policies: Sequence[str] = (),
    params: Sequence[str] = (),
    overrides_json: str | None = None,
) -> dict[str, Any]:
    """The overrides object for ``ORSIM_POLICY_OVERRIDES``.

    Starts from ``--policy-overrides`` (:func:`parse_policy_overrides_json`, entries
    copied whole so unknown keys reach the validating merge), then adds
    ``--policy``/``--policy-param`` and ``--solver X`` (= ``--policy dispatch=X``).
    The same field (a slot's ``type``, ``timing`` or one param key) set to two
    different values by two sources raises ``PolicyError``; equal values are fine.
    """
    from apps.container_logistics.policies.framework import PolicyError, parse_policy_cli_args

    out = {slot: copy.deepcopy(entry) for slot, entry in parse_policy_overrides_json(overrides_json).items()}
    cli = parse_policy_cli_args(list(policies or ()), list(params or ()))
    for slot, entry in cli.items():
        target = out.setdefault(slot, {})
        if "type" in entry:
            _set_once(target, "type", entry["type"], f"{slot}.type", "--policy")
        if entry.get("params"):
            bucket = target.setdefault("params", {})
            if not isinstance(bucket, dict):
                raise PolicyError(f"--policy-overrides.{slot}.params must be an object (got {bucket!r}).")
            for key, value in entry["params"].items():
                _set_once(bucket, key, value, f"{slot}.params.{key}", "--policy-param")
    solver = (solver or "").strip()
    if solver:
        current = (out.get("dispatch") or {}).get("type")
        if current not in (None, solver):
            raise PolicyError(
                f"--solver {solver!r} conflicts with dispatch type {current!r} from "
                "--policy/--policy-overrides; --solver is shorthand for --policy dispatch=NAME, "
                "pass one of them."
            )
        out.setdefault("dispatch", {})["type"] = solver
    return out


def load_assignment_collection(scenario: str | None) -> dict[str, Any] | None:
    """The scenario's compiled assignment behaviors, read-only; ``None`` when there is no bundle.

    Reads ``scenario.json`` (or a legacy ``assignment_behavior.json``) from the same
    folder ``ScenarioManager`` loads at run start; never generates anything.
    """
    folder = _scenario_folder(scenario)
    from apps.container_logistics.scenario.scenario_bundle import read_bundle

    bundle = read_bundle(folder) if os.path.isdir(folder) else None
    agents = (bundle or {}).get("agents")
    if isinstance(agents, dict) and isinstance(agents.get("assignment"), dict):
        return agents["assignment"]
    legacy = os.path.join(folder, "assignment_behavior.json")
    if os.path.isfile(legacy):
        with open(legacy, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    return None


def _scenario_folder(scenario: str | None) -> str:
    from apps.container_logistics.scenario.frontend_scenario_spec import (
        container_logistics_scenarios_root,
    )
    from apps.container_logistics.scenario.scenario_config import default_scenario_name

    return os.path.join(container_logistics_scenarios_root(), scenario or default_scenario_name())


def _missing_bundle_message(scenario: str | None) -> str:
    folder = _scenario_folder(scenario)
    name = os.path.basename(folder)
    if not os.path.isdir(folder):
        return f"scenario {name!r} not found (no folder {folder}); cannot validate run policies."
    return (
        f"scenario {name!r} has no compiled bundle (scenario.json) in {folder}; compile it "
        f"(openride scenario compile {name}) before validating run policies."
    )


def validate_run_policy_args(
    scenario: str | None,
    solver: str | None,
    policies: Sequence[str] = (),
    params: Sequence[str] = (),
    overrides_json: str | None = None,
) -> dict[str, Any] | str:
    """Dry-run a run's policy flags against the scenario's assignment profile.

    Returns the overrides object to ship (``{}`` when no flag was given; the scenario
    is then not read), or the error message (``str``) when the flags, the merge, or
    the patched profile fail to resolve, or when the scenario has no readable bundle
    (never validated against slot defaults: that would name the wrong policy). A
    bundle compiled before policy slots goes through the legacy translation, exactly
    as at run start.
    """
    from apps.container_logistics.policies.dispatch.profile import (
        assert_assignment_profiles_resolvable,
    )
    from apps.container_logistics.policies.framework import PolicyError
    from apps.container_logistics.scenario.scenario_overrides import apply_policy_overrides

    try:
        overrides = build_run_policy_overrides(solver, policies, params, overrides_json)
        if not overrides:
            return {}
        collection = load_assignment_collection(scenario)
        if collection is None:
            return _missing_bundle_message(scenario)
        collection = copy.deepcopy(collection)
        apply_policy_overrides(collection, overrides)
        assert_assignment_profiles_resolvable(collection)
    except PolicyError as exc:
        return str(exc)
    return overrides


def cmd_validate_run_policies(
    scenario: str | None,
    solver: str | None,
    policies: Sequence[str] = (),
    params: Sequence[str] = (),
    overrides_json: str | None = None,
) -> dict[str, Any]:
    """``validate-run-policies`` payload: ``{ok, action, overrides}`` or ``{ok:false, action, message}``.

    The synchronous dry run the dashboard's run route calls before it spawns
    ``run-simulation`` in the background (review F2): the same
    :func:`validate_run_policy_args` ``run-simulation`` runs.
    """
    action = "validate_run_policies"
    result = validate_run_policy_args(scenario, solver, policies, params, overrides_json)
    if isinstance(result, str):
        return {"ok": False, "action": action, "message": result}
    return {"ok": True, "action": action, "overrides": result}


def validate_capture_arg(text: str | None) -> str | None:
    """``None`` when ``text`` is unset or a valid ``every=N,max=M`` spec, else the error message."""
    from apps.container_logistics.policies.dispatch.capture import parse_capture_spec
    from apps.container_logistics.policies.framework import PolicyError

    if text is None or not str(text).strip():
        return None
    try:
        parse_capture_spec(text)
    except PolicyError as exc:
        return f"--capture-dispatch: {exc}"
    return None


def _cli_value(raw: str) -> Any:
    # Same coercion as ``--policy-param`` (framework.resolve): JSON when it parses.
    try:
        return json.loads(raw)
    except ValueError:
        return raw


def parse_bench_params(names: Sequence[str], args: Sequence[str] = ()) -> dict[str, dict[str, Any]]:
    """``POLICY.KEY=VALUE`` strings -> ``{policy: {key: value}}`` (shape only).

    VALUE is JSON when it parses, else the raw string. Raises ``PolicyError`` for a
    malformed argument, a repeated key, or a policy that is not in ``names``.
    """
    from apps.container_logistics.policies.framework import PolicyError

    out: dict[str, dict[str, Any]] = {}
    for arg in args or ():
        key_part, sep, value = str(arg).partition("=")
        name, dot, key = key_part.strip().partition(".")
        if not sep or not dot or not name or not key:
            raise PolicyError(
                f"--policy-param {arg!r} must be POLICY.KEY=VALUE, e.g. GreedyNearest.dual_cycle_bonus_km=0."
            )
        if name not in names:
            raise PolicyError(f"--policy-param {arg!r} names {name!r}, which is not benched ({list(names)}).")
        bucket = out.setdefault(name, {})
        if key in bucket:
            raise PolicyError(f"--policy-param sets {name}.{key} twice; pass it once.")
        bucket[key] = _cli_value(value)
    return out


def _check_dict(result: Any) -> dict[str, Any]:
    return {"id": result.id, "group": result.group, "check": result.check,
            "ok": result.ok, "detail": result.detail, "ms": result.ms}


def cmd_policy_validate(slot: str, name: str) -> dict[str, Any]:
    """``{ok, action, slot, name, results:[{id, group, check, ok, detail, ms}, ...]}``.

    ``ok`` is false when any check failed. A slot or name that cannot be validated
    (unknown, not implemented) gives ``{ok: false, code: "INVALID", message}`` and no
    ``results``.
    """
    from apps.container_logistics.policies.framework import PolicyError
    from apps.container_logistics.policies.testing.battery import run_battery

    action = "policy_validate"
    try:
        results = run_battery(slot, name)
    except PolicyError as exc:
        return {"ok": False, "action": action, "code": "INVALID", "message": str(exc)}
    failed = [r.id for r in results if not r.ok]
    return {
        "ok": not failed,
        "action": action,
        "slot": slot,
        "name": name,
        "results": [_check_dict(r) for r in results],
        "message": (f"{len(failed)} check(s) failed: {', '.join(failed)}" if failed
                    else f"all {len(results)} checks passed"),
    }


def _synthetic(names: str) -> list[Any]:
    from apps.container_logistics.policies.framework import PolicyError
    from apps.container_logistics.policies.testing import fixtures as fx

    makers = {make.__name__: make for make in fx.ROBUSTNESS_FIXTURES}
    wanted = [n.strip() for n in names.split(",") if n.strip()]
    if wanted == ["all"]:
        return fx.synthetic_fixtures()
    unknown = [n for n in wanted if n not in makers]
    if not wanted or unknown:
        raise PolicyError(
            f"--synthetic {names!r}: unknown fixture(s) {unknown or [names]}; "
            f"choose from {sorted(makers)} (comma-separated) or 'all'."
        )
    return [makers[n]() for n in wanted]


def cmd_policy_bench(
    slot: str,
    policies: str,
    *,
    fixtures_dir: str | None = None,
    synthetic: str | None = None,
    params: Sequence[str] = (),
) -> dict[str, Any]:
    """``{ok, action, report: BenchReport.to_dict(), table: str}`` for ``policies`` (comma list).

    Exactly one of ``fixtures_dir`` (captured ``step_*.json.gz``, via
    ``bench.load_fixtures``) and ``synthetic`` (fixture names or ``all``) is required.
    Any refusal (bad slot, unknown policy, bad params, empty fixture set) gives
    ``{ok: false, code: "INVALID", message}``.
    """
    from apps.container_logistics.policies.framework import PolicyError, get_policy_spec
    from apps.container_logistics.policies.testing.bench import bench, load_fixtures, render_table

    action = "policy_bench"
    try:
        if slot != "dispatch":
            raise PolicyError(f"no bench for slot {slot!r}; only 'dispatch' has one.")
        names = [n.strip() for n in str(policies or "").split(",") if n.strip()]
        if not names:
            raise PolicyError("--policies needs at least one policy name, e.g. GreedyNearest,RandomAssignment.")
        for name in names:
            get_policy_spec(slot, name)
        if (fixtures_dir is None) == (synthetic is None):
            raise PolicyError("pass exactly one of --fixtures DIR or --synthetic NAME[,NAME]|all.")
        fixtures = load_fixtures(fixtures_dir) if fixtures_dir is not None else _synthetic(synthetic or "")
        if not fixtures:
            raise PolicyError(f"no step_*.json.gz fixtures in {fixtures_dir!r}.")
        report = bench(names, fixtures, parse_bench_params(names, params))
    except (PolicyError, FileNotFoundError) as exc:
        return {"ok": False, "action": action, "code": "INVALID", "message": str(exc)}
    return {"ok": True, "action": action, "report": report.to_dict(), "table": render_table(report)}
