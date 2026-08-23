"""Thin wrappers over ``python -m openride_control.command`` (run with the apps venv).

All scenario/service logic lives in apps code (imports ``apps.*``), which only the apps
venv can import — so the CLI shells out exactly like the dashboard's host-control.ts does,
and parses the JSON the command emits. One JSON object per line; we take the last parseable
line. Anything that fails surfaces as ``ControlError`` (carrying the command's message).
"""

from __future__ import annotations

import json
import os
import subprocess
from typing import Any, Optional

from openride_control.paths import ROOT, openride_apps_env

from . import config


class ControlError(RuntimeError):
    """A control-plane command failed.

    ``code`` carries the control plane's machine-readable reason when it gave one
    (e.g. ``EXISTS``, ``INVALID``, ``COOP_RESET_REQUIRED``), so callers can branch
    on it instead of string-matching the message.
    """

    code: str | None = None


def _run_apps_python(
    cmd: list[str],
    *,
    label: str,
    timeout: float = 60.0,
    input_text: Optional[str] = None,
) -> dict[str, Any]:
    """Run ``cmd`` under the apps venv and return the last JSON object it printed.

    Shared by every apps-venv call: the control-plane subcommands and the one
    inline probe below. ``label`` is what the error message names, so a failure
    reads as the *operation* that failed rather than an argv dump.
    """
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(ROOT),
            env=openride_apps_env(),
            capture_output=True,
            text=True,
            timeout=timeout,
            input=input_text,
        )
    except subprocess.TimeoutExpired as exc:
        raise ControlError(f"control command timed out: {label}") from exc

    payload: dict[str, Any] | None = None
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
    if payload is None:
        raise ControlError(
            f"no JSON from control command {label} (exit {proc.returncode}): "
            f"{proc.stderr.strip()[:400]}"
        )
    return payload


def _run_command(
    args: list[str], *, timeout: float = 60.0, input_text: Optional[str] = None
) -> dict[str, Any]:
    return _run_apps_python(
        [str(config.APPS_PYTHON), "-m", "openride_control.command", *args],
        label=repr(args),
        timeout=timeout,
        input_text=input_text,
    )


def _ok(payload: dict[str, Any]) -> dict[str, Any]:
    """Return the payload if ok, else raise ControlError carrying its message.

    The control plane's ``code`` is attached to the exception rather than
    discarded: a caller needs it to distinguish a recoverable refusal (e.g.
    ``COOP_RESET_REQUIRED``, which has an explicit opt-in) from a hard failure.
    String-matching the message is not the contract.
    """
    if not payload.get("ok"):
        err = ControlError(payload.get("message") or "control command failed")
        err.code = payload.get("code")
        raise err
    return payload


def _domain_args(domain: str | None) -> list[str]:
    return ["--domain", domain] if domain else []


# -- scenarios ------------------------------------------------------------


def list_scenarios(domain: str | None = None) -> list[dict[str, Any]]:
    payload = _run_command(["list-scenarios", *_domain_args(domain)])
    scenarios = payload.get("scenarios")
    return scenarios if isinstance(scenarios, list) else []


def get_scenario(slug: str, domain: str | None = None) -> dict[str, Any]:
    payload = _ok(_run_command(["get-scenario", "--slug", slug, *_domain_args(domain)]))
    return payload.get("scenario") or {}


def scenario_exists(slug: str, domain: str | None = None) -> bool:
    payload = _run_command(["scenario-exists", "--slug", slug, *_domain_args(domain)])
    return bool(payload.get("exists") or payload.get("runnable"))


def generate_scenario(
    spec: dict[str, Any],
    *,
    overwrite: bool = False,
    domain: str | None = None,
    allow_cooperation_reset: bool = False,
) -> dict[str, Any]:
    """Generate a scenario from a spec.

    ``allow_cooperation_reset`` opts in to EMPTYING a structure's cooperation
    content; without it the control plane refuses with ``COOP_RESET_REQUIRED`` so a
    client that does not understand a cooperation field cannot silently delete it.
    """
    args = ["generate-scenario"]
    if overwrite:
        args.append("--overwrite")
    if allow_cooperation_reset:
        args.append("--allow-cooperation-reset")
    body = {**spec}
    if domain:
        body.setdefault("domain", domain)
    payload = _ok(_run_command(args, input_text=json.dumps(body), timeout=300.0))
    return payload.get("scenario") or {}


def compile_scenario(slug: str, domain: str | None = None, *, reseed: bool = False) -> dict[str, Any]:
    args = ["compile-scenario", "--slug", slug, *_domain_args(domain)]
    if reseed:
        args.append("--reseed")
    payload = _ok(_run_command(args, timeout=300.0))
    return payload.get("scenario") or {}


def get_spec(slug: str, domain: str | None = None) -> dict[str, Any]:
    """Return the folder's raw ``spec.json`` recipe (the faithful, editable source)."""
    payload = _ok(_run_command(["get-spec", "--slug", slug, *_domain_args(domain)]))
    return payload.get("spec") or {}


# -- facilityRules baseline ----------------------------------------------
#
# `openride scenario rules-baseline` needs two things the control-plane
# subcommands do not expose: the facility sites a spec generates *without*
# writing a bundle, and the authored rules evaluated against them. Both live in
# apps code (venv-only), so this shells out exactly like every other call here —
# the difference is only that it runs a probe instead of a registered subcommand.
#
# The probe compiles a COPY of the spec with facilityRules/facilityRulesWorld
# removed. Compiling the spec as authored would raise the very staleness guard
# this command exists to move forward. It writes nothing: `Preprocessor.compile`
# only reads (it resolves `$file` refs out of the scenario folder), and neither
# `ScenarioGenerator` nor `write_bundle` runs. The spec.json rewrite is done
# afterwards, here on the CLI side, so exactly one key is touched.
_FACILITY_RULES_PROBE = r'''
import json, os, sys

def main(req):
    from apps.config import simulation_domains
    from apps.container_logistics.datagen import facility_rules as fr
    from apps.container_logistics.datagen.preprocess import Preprocessor
    from apps.container_logistics.scenario.frontend_scenario_spec import (
        read_spec, scenario_dir, validate_slug,
    )
    from datetime import datetime, timezone

    slug = str(req.get("slug") or "")
    domain = req.get("domain") or simulation_domains["container_logistics"]
    slug_err = validate_slug(slug)
    if slug_err:
        return {"ok": False, "message": slug_err}
    path = scenario_dir("", domain, slug)
    if not os.path.isdir(path):
        return {"ok": False, "message": "Scenario not found: %s" % slug}
    raw = read_spec(path)
    if not isinstance(raw, dict):
        return {"ok": False, "message": "No spec.json to baseline for scenario: %s" % slug}

    # The world is a property of the spec MINUS its rules: with them in, the
    # staleness guard fires before any site list comes back.
    baseline = {k: v for k, v in raw.items()
                if k not in ("facilityRules", "facilityRulesWorld")}
    kwargs = {}
    declared = raw.get("referenceTime")
    if isinstance(declared, str) and declared.strip():
        # Mirrors compile_spec_to_bundle: a declared epoch beats the default.
        kwargs["reference_time"] = declared.strip()
    compiled = Preprocessor.compile(baseline, domain=domain, scenario_dir=path, **kwargs)
    sites = compiled.spec.facilities()

    rules = raw.get("facilityRules")
    reports = []
    for i, rule in enumerate(rules if isinstance(rules, list) else []):
        entry = {"index": i, "label": "facilityRules[%d] (malformed)" % i}
        try:
            entry["label"] = fr.describe_rule(i, rule)
            entry["set_keys"] = sorted(rule.get("set") or {})
            matched = [s for s in sites if fr.rule_matches(rule, s)]
            entry["matched"] = len(matched)
            entry["examples"] = [str(s.get("name")) for s in matched[:4]]
            mkey = next(iter(rule.get("match") or {}), None)
            entry["matcher"] = mkey
            entry["matcher_value"] = (rule.get("match") or {}).get(mkey)
        except Exception as exc:
            entry["error"] = "%s: %s" % (type(exc).__name__, exc)
        reports.append(entry)

    # Reported, never fatal: a rule that no longer matches anything is exactly
    # what the operator is here to SEE. Compile refuses it a moment later.
    validation_error = None
    try:
        fr.validate_facility_rules(
            rules, sites, list(Preprocessor.catalog().codes()), slug=slug
        )
    except Exception as exc:
        validation_error = str(exc)

    stamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    return {
        "ok": True,
        "slug": slug,
        "scenario_path": path,
        "recorded": raw.get("facilityRulesWorld")
                    if isinstance(raw.get("facilityRulesWorld"), dict) else None,
        "now": {
            "site_digest": fr.site_digest(sites),
            "facility_count": len(sites),
            "code_counts": fr.code_counts(sites),
        },
        "rules": reports,
        "rule_count": len(reports),
        "has_rules": isinstance(rules, list) and bool(rules),
        "validation_error": validation_error,
        "snapshot": fr.world_snapshot(
            sites, seed=raw.get("seed"), recorded_at=stamp.replace("+00:00", "Z")
        ),
    }

try:
    out = main(json.loads(sys.stdin.read() or "{}"))
except Exception as exc:
    out = {"ok": False, "message": "%s: %s" % (type(exc).__name__, exc)}
sys.stdout.write(json.dumps(out) + "\n")
'''


def facility_rules_baseline(slug: str, domain: str | None = None) -> dict[str, Any]:
    """Generate the scenario's facility world and evaluate its rules against it.

    Returns ``{scenario_path, recorded, now, rules, validation_error, snapshot}``.
    Reads only — nothing is written and no bundle is compiled; the caller decides
    whether to record ``snapshot`` via :func:`write_facility_rules_world`.
    """
    payload = _run_apps_python(
        [str(config.APPS_PYTHON), "-c", _FACILITY_RULES_PROBE],
        label=f"facility-rules-baseline {slug}",
        input_text=json.dumps({"slug": slug, "domain": domain}),
        timeout=300.0,
    )
    return _ok(payload)


def write_facility_rules_world(scenario_path: str, world: dict[str, Any]) -> None:
    """Rewrite ONLY ``facilityRulesWorld`` in a scenario's ``spec.json``.

    Deliberately NOT ``edit_scenario``: that recompiles, and re-baselining must not
    imply a compile (nor let a compile happen while the rules are still unreviewed).
    Formatting matches ``frontend_scenario_spec.write_spec`` exactly — ``indent=2``,
    ``sort_keys=True``, trailing newline, atomic ``.tmp`` + rename — so a spec that
    round-trips through the dashboard or a recompile is byte-comparable either way.
    """
    target = os.path.join(scenario_path, "spec.json")
    with open(target, "r", encoding="utf-8") as fp:
        spec = json.load(fp)
    if not isinstance(spec, dict):
        raise ControlError(f"{target} is not a JSON object")

    updated = {**spec, "facilityRulesWorld": world}
    # Self-check, not decoration: this command's whole promise is "the rules are
    # untouched". Diffing the two dicts is the only thing that actually holds the
    # writer to it if the shaping above is ever changed.
    changed = [k for k in set(updated) | set(spec) if updated.get(k) != spec.get(k)]
    if changed != ["facilityRulesWorld"]:
        raise ControlError(
            f"refusing to write {target}: the rewrite would change {sorted(changed)}, "
            f"but only 'facilityRulesWorld' may change."
        )

    tmp = target + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fp:
        json.dump(updated, fp, indent=2, sort_keys=True)
        fp.write("\n")
    os.replace(tmp, target)


def edit_scenario(
    slug: str,
    patch: dict[str, Any],
    *,
    domain: str | None = None,
    allow_cooperation_reset: bool = False,
) -> dict[str, Any]:
    """Apply a patch to a scenario's spec.json and recompile.

    ``allow_cooperation_reset`` opts in to EMPTYING a structure's cooperation
    content; without it the control plane refuses such a save with
    ``COOP_RESET_REQUIRED`` so a client that does not understand a cooperation
    field cannot silently delete it (shared-pool plan §13.4 FIX-6).
    """
    payload = _ok(
        _run_command(
            [
                "edit-scenario",
                "--slug",
                slug,
                *_domain_args(domain),
                *(["--allow-cooperation-reset"] if allow_cooperation_reset else []),
            ],
            input_text=json.dumps(patch or {}),
            timeout=300.0,
        )
    )
    return payload.get("scenario") or {}


def stage_sources(
    slug: str,
    *,
    gen_file: str | None = None,
    inputs: Optional[list[str]] = None,
    domain: str | None = None,
) -> dict[str, Any]:
    args = ["stage-sources", "--slug", slug]
    if gen_file:
        args += ["--gen-file", gen_file]
    for path in inputs or []:
        args += ["--input", path]
    return _ok(_run_command(args + _domain_args(domain), timeout=300.0))


def delete_scenario(slug: str, domain: str | None = None) -> None:
    _ok(_run_command(["delete-scenario", "--slug", slug, *_domain_args(domain)]))


def rebuild_index(domain: str | None = None) -> dict[str, Any]:
    return _ok(_run_command(["rebuild-index", *_domain_args(domain)]))


# -- solvers --------------------------------------------------------------


def known_solvers() -> dict[str, Any]:
    """Return ``{solvers: [...], default: str}`` from the live SOLVER_REGISTRY."""
    payload = _ok(_run_command(["known-solvers"]))
    return {"solvers": payload.get("solvers") or [], "default": payload.get("default")}


def known_policies() -> dict[str, Any]:
    """Return ``{policies: {role: [...]}, defaults: {role: str}}`` from the POLICY_REGISTRY."""
    payload = _ok(_run_command(["known-policies"]))
    return {"policies": payload.get("policies") or {}, "defaults": payload.get("defaults") or {}}


# -- services -------------------------------------------------------------


def service_status(service: str | None = None) -> list[dict[str, Any]]:
    args = ["status"]
    if service:
        args += ["--service", service]
    payload = _run_command(args, timeout=40.0)
    services = payload.get("services")
    return services if isinstance(services, list) else []


def start_service(service: str, *, with_dependencies: bool = True) -> dict[str, Any]:
    args = ["start", "--service", service]
    if not with_dependencies:
        args.append("--no-with-dependencies")
    return _run_command(args, timeout=180.0)


def stop_service(service: str, *, force: bool = False) -> dict[str, Any]:
    args = ["stop", "--service", service]
    if force:
        args.append("--force")
    return _run_command(args, timeout=120.0)
