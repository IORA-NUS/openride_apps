"""Scenario list/generate/get commands for analytics API."""

from __future__ import annotations

import json
import sys
from typing import Any

from apps.config import simulation_domains
from apps.container_logistics.scenario.frontend_scenario_spec import (
    compile_scenario,
    delete_scenario,
    edit_scenario,
    CooperationResetRequired,
    generate_scenario,
    get_scenario_detail,
    get_scenario_spec,
    known_policies,
    known_solvers,
    list_scenarios,
    scenario_exists,
    stage_sources,
    validate_slug,
)
from apps.simulation.container_logistics_wiring import get_datahub_dir


def _default_domain() -> str:
    return simulation_domains["container_logistics"]


def cmd_list_scenarios(*, domain: str | None = None) -> dict[str, Any]:
    domain = domain or _default_domain()
    datahub = get_datahub_dir()
    return {"ok": True, "action": "list_scenarios", "scenarios": list_scenarios(datahub, domain)}


def cmd_get_scenario(slug: str, *, domain: str | None = None) -> dict[str, Any]:
    domain = domain or _default_domain()
    slug_err = validate_slug(slug)
    if slug_err:
        return {"ok": False, "action": "get_scenario", "message": slug_err}
    try:
        detail = get_scenario_detail(get_datahub_dir(), domain, slug)
        return {"ok": True, "action": "get_scenario", "scenario": detail}
    except FileNotFoundError as exc:
        return {"ok": False, "action": "get_scenario", "message": str(exc)}
    except ValueError as exc:
        return {"ok": False, "action": "get_scenario", "message": str(exc)}


def cmd_generate_scenario(
    spec: dict[str, Any],
    *,
    overwrite: bool = False,
    allow_cooperation_reset: bool = False,
) -> dict[str, Any]:
    domain = spec.get("domain") if isinstance(spec.get("domain"), str) else _default_domain()
    try:
        entry = generate_scenario(
            get_datahub_dir(),
            domain,
            spec,
            overwrite=overwrite,
            allow_cooperation_reset=allow_cooperation_reset,
        )
        return {
            "ok": True,
            "action": "generate_scenario",
            "message": f"Scenario {entry['slug']} generated",
            "scenario": entry,
        }
    except FileExistsError as exc:
        return {"ok": False, "action": "generate_scenario", "message": str(exc), "code": "EXISTS"}
    except CooperationResetRequired as exc:
        # Distinct code so the UI can offer an explicit "clear it on purpose"
        # retry instead of dead-ending (shared-pool plan §13.4 FIX-6).
        return {
            "ok": False,
            "action": "generate_scenario",
            "message": str(exc),
            "code": "COOP_RESET_REQUIRED",
        }
    except ValueError as exc:
        return {"ok": False, "action": "generate_scenario", "message": str(exc), "code": "INVALID"}
    except Exception as exc:
        return {"ok": False, "action": "generate_scenario", "message": str(exc)}


def cmd_delete_scenario(slug: str, *, domain: str | None = None) -> dict[str, Any]:
    domain = domain or _default_domain()
    slug_err = validate_slug(slug)
    if slug_err:
        return {"ok": False, "action": "delete_scenario", "message": slug_err}
    try:
        delete_scenario(get_datahub_dir(), domain, slug)
        return {
            "ok": True,
            "action": "delete_scenario",
            "message": f"Scenario {slug} deleted",
            "slug": slug,
        }
    except FileNotFoundError as exc:
        return {"ok": False, "action": "delete_scenario", "message": str(exc)}
    except ValueError as exc:
        return {"ok": False, "action": "delete_scenario", "message": str(exc), "code": "PROTECTED"}


def cmd_compile_scenario(slug: str, *, domain: str | None = None, reseed: bool = False) -> dict[str, Any]:
    domain = domain or _default_domain()
    slug_err = validate_slug(slug)
    if slug_err:
        return {"ok": False, "action": "compile_scenario", "message": slug_err}
    try:
        entry = compile_scenario(get_datahub_dir(), domain, slug, reseed=reseed)
        return {
            "ok": True,
            "action": "compile_scenario",
            "message": f"Scenario {slug} {'regenerated (new seed)' if reseed else 'compiled'}",
            "scenario": entry,
        }
    except FileNotFoundError as exc:
        return {"ok": False, "action": "compile_scenario", "message": str(exc)}
    except ValueError as exc:
        return {"ok": False, "action": "compile_scenario", "message": str(exc), "code": "INVALID"}
    except Exception as exc:
        return {"ok": False, "action": "compile_scenario", "message": str(exc)}


def cmd_rebuild_index(*, domain: str | None = None) -> dict[str, Any]:
    domain = domain or _default_domain()
    try:
        from apps.container_logistics.scenario.scenario_index import rebuild_rollup

        result = rebuild_rollup(get_datahub_dir(), domain)
        return {"ok": True, "action": "rebuild_index", **result}
    except Exception as exc:
        return {"ok": False, "action": "rebuild_index", "message": str(exc)}


def cmd_get_spec(slug: str, *, domain: str | None = None) -> dict[str, Any]:
    domain = domain or _default_domain()
    slug_err = validate_slug(slug)
    if slug_err:
        return {"ok": False, "action": "get_spec", "message": slug_err}
    try:
        spec = get_scenario_spec(get_datahub_dir(), domain, slug)
        return {"ok": True, "action": "get_spec", "slug": slug, "spec": spec}
    except FileNotFoundError as exc:
        return {"ok": False, "action": "get_spec", "message": str(exc)}
    except ValueError as exc:
        return {"ok": False, "action": "get_spec", "message": str(exc)}


def cmd_edit_scenario(
    slug: str,
    patch: dict[str, Any],
    *,
    domain: str | None = None,
    allow_cooperation_reset: bool = False,
) -> dict[str, Any]:
    domain = domain or _default_domain()
    slug_err = validate_slug(slug)
    if slug_err:
        return {"ok": False, "action": "edit_scenario", "message": slug_err}
    try:
        entry = edit_scenario(
            get_datahub_dir(),
            domain,
            slug,
            patch,
            allow_cooperation_reset=allow_cooperation_reset,
        )
        return {
            "ok": True,
            "action": "edit_scenario",
            "message": f"Scenario {slug} edited",
            "scenario": entry,
        }
    except FileNotFoundError as exc:
        return {"ok": False, "action": "edit_scenario", "message": str(exc)}
    except CooperationResetRequired as exc:
        # Must precede the ValueError arm — CooperationResetRequired subclasses it,
        # and a bare "INVALID" would strand the caller with no way to opt in
        # (shared-pool plan §13.4 FIX-6).
        return {
            "ok": False,
            "action": "edit_scenario",
            "message": str(exc),
            "code": "COOP_RESET_REQUIRED",
        }
    except ValueError as exc:
        return {"ok": False, "action": "edit_scenario", "message": str(exc), "code": "INVALID"}
    except Exception as exc:
        return {"ok": False, "action": "edit_scenario", "message": str(exc)}


def cmd_stage_sources(
    slug: str,
    *,
    gen_file: str | None = None,
    inputs: list[str] | None = None,
    domain: str | None = None,
) -> dict[str, Any]:
    domain = domain or _default_domain()
    slug_err = validate_slug(slug)
    if slug_err:
        return {"ok": False, "action": "stage_sources", "message": slug_err}
    try:
        result = stage_sources(
            get_datahub_dir(), domain, slug, gen_file=gen_file, inputs=inputs
        )
        return {
            "ok": True,
            "action": "stage_sources",
            "message": f"Sources staged for {slug}",
            **result,
        }
    except FileNotFoundError as exc:
        return {"ok": False, "action": "stage_sources", "message": str(exc)}
    except ValueError as exc:
        return {"ok": False, "action": "stage_sources", "message": str(exc), "code": "INVALID"}
    except Exception as exc:
        return {"ok": False, "action": "stage_sources", "message": str(exc)}


def cmd_known_solvers() -> dict[str, Any]:
    return {"ok": True, "action": "known_solvers", **known_solvers()}


def cmd_known_policies() -> dict[str, Any]:
    return {"ok": True, "action": "known_policies", **known_policies()}


def cmd_scenario_exists(slug: str, *, domain: str | None = None) -> dict[str, Any]:
    domain = domain or _default_domain()
    slug_err = validate_slug(slug)
    if slug_err:
        return {"ok": False, "action": "scenario_exists", "message": slug_err, "exists": False}
    exists = scenario_exists(get_datahub_dir(), domain, slug)
    return {"ok": True, "action": "scenario_exists", "exists": exists, "slug": slug}
