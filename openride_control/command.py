"""Host-side service control CLI (invoked from analytics API routes)."""

from __future__ import annotations

import argparse
import json
import sys
import uuid

from openride_control.manager import SIMULATION_PROCESS_KEY, ServiceManager
from openride_control.models import ActionResult
from openride_control.registry import SERVICES


def _result_payload(
    result: ActionResult,
    *,
    command_id: str,
    action: str,
) -> dict:
    return {
        "commandId": command_id,
        "ok": result.ok,
        "action": action,
        "service": result.key,
        "message": result.message,
        "blockedBy": result.blocked_by or [],
        "state": result.state.value,
    }


def _emit(payload: dict) -> None:
    json.dump(payload, sys.stdout)
    sys.stdout.write("\n")


def _run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="OpenRide host service control")
    sub = parser.add_subparsers(dest="cmd", required=True)

    status_p = sub.add_parser("status", help="JSON snapshot of all services")
    status_p.add_argument(
        "--service",
        default=None,
        help="Probe only this service key (skips the multi-service fan-out, e.g. the "
        "~3s celery inspect ping) — used by hot-path callers that read just one row",
    )

    start_p = sub.add_parser("start", help="Start one service")
    start_p.add_argument("--service", required=True)
    start_p.add_argument(
        "--with-dependencies",
        action=argparse.BooleanOptionalAction,
        default=True,
    )

    stop_p = sub.add_parser("stop", help="Stop one service")
    stop_p.add_argument("--service", required=True)
    stop_p.add_argument("--force", action="store_true")

    sub.add_parser("start-backend", help="Start full backend stack in order")

    stop_all_p = sub.add_parser("stop-all", help="Stop all backend services")
    stop_all_p.add_argument("--force", action="store_true", default=True)

    run_p = sub.add_parser("run-simulation", help="Start simulation in background")
    run_p.add_argument(
        "--scenario",
        default=None,
        help="ORSIM_SCENARIO override (default: last generated on disk)",
    )
    run_p.add_argument(
        "--run-name",
        default=None,
        help="Human-readable run name (ORSIM_RUN_NAME)",
    )
    run_p.add_argument(
        "--run-id",
        default=None,
        help="Pre-assigned run id (ORSIM_RUN_ID); lets the caller attach without a discovery race",
    )
    run_p.add_argument(
        "--solver",
        default=None,
        help="Assignment solver strategy override (ORSIM_SOLVER): RandomAssignment | GreedyNearest",
    )
    run_p.add_argument(
        "--cooperation-structure",
        default=None,
        help="Cooperation structure id override (ORSIM_COOP_STRUCTURE): a structure "
        "declared in the scenario's spec.json cooperation block",
    )
    run_p.add_argument(
        "--sharing-policy",
        default=None,
        help="Sharing algorithm override for two-stage planners (ORSIM_SHARING_POLICY)",
    )
    run_p.add_argument(
        "--order-lifecycle",
        choices=["agents", "service"],
        default=None,
        help="Order lifecycle mode: 'agents' (per-order agents, default) or 'service' "
        "(one order-lifecycle service agent).",
    )

    list_sc_p = sub.add_parser("list-scenarios", help="List scenario metadata on disk")
    list_sc_p.add_argument("--domain", default=None)

    get_sc_p = sub.add_parser("get-scenario", help="Get scenario metadata and preview sample")
    get_sc_p.add_argument("--slug", required=True)
    get_sc_p.add_argument("--domain", default=None)

    gen_sc_p = sub.add_parser("generate-scenario", help="Generate a scenario from JSON spec on stdin")
    gen_sc_p.add_argument("--overwrite", action="store_true")
    # Explicit opt-in to EMPTYING a scenario's cooperation structures. The guard in
    # generate_scenario refuses a silent reduction to empty (shared-pool plan §13.4
    # FIX-6 layer 2); this is the only way past it, so a deliberate reset stays
    # possible while an editor that simply does not know about a field cannot
    # delete it.
    gen_sc_p.add_argument("--allow-cooperation-reset", action="store_true")

    compile_sc_p = sub.add_parser(
        "compile-scenario", help="Preprocess a scenario folder's sources (spec.json) into scenario.json"
    )
    compile_sc_p.add_argument("--slug", required=True)
    compile_sc_p.add_argument("--domain", default=None)
    compile_sc_p.add_argument(
        "--reseed", action="store_true",
        help="Draw a fresh master seed (persisted) so regeneration produces NEW data",
    )

    getspec_sc_p = sub.add_parser("get-spec", help="Print a scenario's raw spec.json recipe")
    getspec_sc_p.add_argument("--slug", required=True)
    getspec_sc_p.add_argument("--domain", default=None)

    edit_sc_p = sub.add_parser(
        "edit-scenario",
        help="Patch a scenario's recipe (JSON patch on stdin) and recompile, keeping its sources",
    )
    edit_sc_p.add_argument("--slug", required=True)
    edit_sc_p.add_argument("--domain", default=None)
    # Same opt-in as generate-scenario: the merge is shallow, so a patch that carries a
    # `cooperation` key replaces the whole block and can empty it (§13.4 FIX-6 layer 2).
    edit_sc_p.add_argument("--allow-cooperation-reset", action="store_true")

    stage_sc_p = sub.add_parser(
        "stage-sources",
        help="Copy a scenario_gen.py override and/or inputs/ files into a scenario folder, then recompile",
    )
    stage_sc_p.add_argument("--slug", required=True)
    stage_sc_p.add_argument("--gen-file", default=None, help="Path to a scenario_gen.py override module")
    stage_sc_p.add_argument(
        "--input", dest="inputs", action="append", default=[], help="Path to a raw input file (repeatable)"
    )
    stage_sc_p.add_argument("--domain", default=None)

    sub.add_parser("known-solvers", help="List runtime-selectable assignment solver strategies")
    sub.add_parser("known-policies", help="List selectable generation policies per role")

    exists_sc_p = sub.add_parser("scenario-exists", help="Check whether a scenario slug is runnable")
    exists_sc_p.add_argument("--slug", required=True)
    exists_sc_p.add_argument("--domain", default=None)

    del_sc_p = sub.add_parser("delete-scenario", help="Delete a scenario dataset folder")
    del_sc_p.add_argument("--slug", required=True)
    del_sc_p.add_argument("--domain", default=None)

    reindex_p = sub.add_parser("rebuild-index", help="Rebuild the dashboard scenario browse-index")
    reindex_p.add_argument("--domain", default=None)

    sub.add_parser("stop-simulation", help="Stop background simulation")

    args = parser.parse_args(argv)
    mgr = ServiceManager()
    command_id = str(uuid.uuid4())

    if args.cmd == "status":
        if args.service:
            services = (
                [mgr.snapshot(args.service).to_dict()]
                if args.service in SERVICES
                else []
            )
        else:
            services = [s.to_dict() for s in mgr.all_snapshots()]
        _emit(
            {
                "commandId": command_id,
                "ok": True,
                "action": "status",
                "services": services,
            }
        )
        return 0

    if args.cmd == "start-backend":
        results = mgr.start_backend()
        ok = all(r.ok for r in results)
        _emit(
            {
                "commandId": command_id,
                "ok": ok,
                "action": "start_backend",
                "service": None,
                "message": "Backend start finished" if ok else "Some services failed to start",
                "results": [_result_payload(r, command_id=command_id, action="start") for r in results],
            }
        )
        return 0 if ok else 1

    if args.cmd == "stop-all":
        results = mgr.stop_all(force=args.force)
        ok = all(r.ok for r in results)
        _emit(
            {
                "commandId": command_id,
                "ok": ok,
                "action": "stop_all",
                "service": None,
                "message": "Stop all finished" if ok else "Some services failed to stop",
                "results": [_result_payload(r, command_id=command_id, action="stop") for r in results],
            }
        )
        return 0 if ok else 1

    if args.cmd == "run-simulation":
        result = mgr.run_simulation_background(
            scenario=args.scenario,
            run_name=args.run_name,
            run_id=args.run_id,
            solver=args.solver,
            cooperation_structure=args.cooperation_structure,
            sharing_policy=args.sharing_policy,
            order_lifecycle=args.order_lifecycle,
        )
        _emit(_result_payload(result, command_id=command_id, action="run_simulation"))
        return 0 if result.ok else 1

    if args.cmd == "list-scenarios":
        from openride_control.scenario import cmd_list_scenarios

        payload = cmd_list_scenarios(domain=args.domain)
        payload["commandId"] = command_id
        _emit(payload)
        return 0 if payload.get("ok") else 1

    if args.cmd == "get-scenario":
        from openride_control.scenario import cmd_get_scenario

        payload = cmd_get_scenario(args.slug, domain=args.domain)
        payload["commandId"] = command_id
        _emit(payload)
        return 0 if payload.get("ok") else 1

    if args.cmd == "generate-scenario":
        from openride_control.scenario import cmd_generate_scenario

        raw = sys.stdin.read()
        try:
            spec = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError as exc:
            _emit(
                {
                    "commandId": command_id,
                    "ok": False,
                    "action": "generate_scenario",
                    "message": f"Invalid JSON: {exc}",
                }
            )
            return 1
        payload = cmd_generate_scenario(
            spec,
            overwrite=args.overwrite,
            allow_cooperation_reset=args.allow_cooperation_reset,
        )
        payload["commandId"] = command_id
        _emit(payload)
        return 0 if payload.get("ok") else 1

    if args.cmd == "compile-scenario":
        from openride_control.scenario import cmd_compile_scenario

        payload = cmd_compile_scenario(args.slug, domain=args.domain, reseed=args.reseed)
        payload["commandId"] = command_id
        _emit(payload)
        return 0 if payload.get("ok") else 1

    if args.cmd == "rebuild-index":
        from openride_control.scenario import cmd_rebuild_index

        payload = cmd_rebuild_index(domain=args.domain)
        payload["commandId"] = command_id
        _emit(payload)
        return 0 if payload.get("ok") else 1

    if args.cmd == "get-spec":
        from openride_control.scenario import cmd_get_spec

        payload = cmd_get_spec(args.slug, domain=args.domain)
        payload["commandId"] = command_id
        _emit(payload)
        return 0 if payload.get("ok") else 1

    if args.cmd == "edit-scenario":
        from openride_control.scenario import cmd_edit_scenario

        raw = sys.stdin.read()
        try:
            patch = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError as exc:
            _emit(
                {
                    "commandId": command_id,
                    "ok": False,
                    "action": "edit_scenario",
                    "message": f"Invalid JSON: {exc}",
                }
            )
            return 1
        payload = cmd_edit_scenario(
            args.slug,
            patch,
            domain=args.domain,
            allow_cooperation_reset=args.allow_cooperation_reset,
        )
        payload["commandId"] = command_id
        _emit(payload)
        return 0 if payload.get("ok") else 1

    if args.cmd == "stage-sources":
        from openride_control.scenario import cmd_stage_sources

        payload = cmd_stage_sources(
            args.slug, gen_file=args.gen_file, inputs=args.inputs, domain=args.domain
        )
        payload["commandId"] = command_id
        _emit(payload)
        return 0 if payload.get("ok") else 1

    if args.cmd == "known-solvers":
        from openride_control.scenario import cmd_known_solvers

        payload = cmd_known_solvers()
        payload["commandId"] = command_id
        _emit(payload)
        return 0 if payload.get("ok") else 1

    if args.cmd == "known-policies":
        from openride_control.scenario import cmd_known_policies

        payload = cmd_known_policies()
        payload["commandId"] = command_id
        _emit(payload)
        return 0 if payload.get("ok") else 1

    if args.cmd == "scenario-exists":
        from openride_control.scenario import cmd_scenario_exists

        payload = cmd_scenario_exists(args.slug, domain=args.domain)
        payload["commandId"] = command_id
        _emit(payload)
        return 0 if payload.get("ok") else 1

    if args.cmd == "delete-scenario":
        from openride_control.scenario import cmd_delete_scenario

        payload = cmd_delete_scenario(args.slug, domain=args.domain)
        payload["commandId"] = command_id
        _emit(payload)
        return 0 if payload.get("ok") else 1

    if args.cmd == "stop-simulation":
        result = mgr.stop_simulation_command()
        _emit(_result_payload(result, command_id=command_id, action="stop_simulation"))
        return 0 if result.ok else 1

    if args.cmd == "start":
        service = args.service.strip()
        if service not in SERVICES:
            _emit(
                {
                    "commandId": command_id,
                    "ok": False,
                    "action": "start",
                    "service": service,
                    "message": f"Unknown service: {service!r}",
                }
            )
            return 1
        result = mgr.start(service, with_dependencies=args.with_dependencies)
        _emit(_result_payload(result, command_id=command_id, action="start"))
        return 0 if result.ok else 1

    if args.cmd == "stop":
        service = args.service.strip()
        if service not in SERVICES:
            _emit(
                {
                    "commandId": command_id,
                    "ok": False,
                    "action": "stop",
                    "service": service,
                    "message": f"Unknown service: {service!r}",
                }
            )
            return 1
        result = mgr.stop(service, force=args.force)
        _emit(_result_payload(result, command_id=command_id, action="stop"))
        return 0 if result.ok else 1

    return 1


def main(argv: list[str] | None = None) -> None:
    sys.exit(_run(argv))


if __name__ == "__main__":
    main()
