"""``openride policy ...`` — list, inspect, validate and bench pluggable policies.

Single responsibility: render the control plane's policy surface (``known-policies``
``slots``, ``policy-validate``, ``policy-bench``) in the terminal. Every name, schema
and default comes from the engine's catalog through :mod:`openride.control`; nothing
here hand-copies a policy list (``tests/test_cli_no_hardcoded_policy_lists.py``).
``openride solver list`` is an alias for ``policy list dispatch``
(:func:`render_policy_list`). Authoring guide: ``docs/policy_authoring.md``.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from rich.markup import escape
from rich.panel import Panel
from rich.table import Table

from . import control
from .ui import console

__all__ = ["register", "render_policy_list"]

_BOUND_KEYS = (("minimum", ">="), ("exclusiveMinimum", ">"), ("maximum", "<="), ("exclusiveMaximum", "<"))


def _dump(obj: Any) -> None:
    json.dump(obj, sys.stdout, indent=2)
    sys.stdout.write("\n")


def _pick_slots(catalog: dict[str, Any], slot: str | None) -> dict[str, Any]:
    if slot is None:
        return catalog
    if slot not in catalog:
        raise ValueError(f"unknown policy slot {slot!r}; slots: {', '.join(catalog)}")
    return {slot: catalog[slot]}


def _implemented_rows(tbl: Table, slot: str, entry: dict[str, Any]) -> None:
    for p in entry.get("policies") or []:
        name = p.get("name", "?")
        default = "✓" if name == entry.get("default") else ""
        missing = p.get("missing") or []
        status = f"[red]missing: {', '.join(missing)}[/]" if missing else "[green]available[/]"
        cell = f"[bold]{name}[/] v{p.get('version', '?')}\n[dim]{escape(str(p.get('description') or ''))}[/]"
        tbl.add_row(slot, cell, default, status)


def _muted_rows(tbl: Table, slot: str, entry: dict[str, Any]) -> None:
    status = entry.get("status")
    if status == "legacy":
        names = "\n".join(entry.get("names") or [])
        note = f"default {entry.get('default')}; configured via {entry.get('configured_via')}"
        tbl.add_row(f"[dim]{slot}[/]", f"[dim]{names}\n{note}[/]", "", "[dim]legacy[/]")
        return
    note = f"{entry.get('kind', '')} slot, lands in {entry.get('lands_in', '?')}"
    tbl.add_row(f"[dim]{slot}[/]", f"[dim]— {note}[/]", "", f"[dim]{status}[/]")


def render_policy_list(slot: str | None = None, *, as_json: bool = False) -> int:
    """Print the catalog (one slot or all). Raises ``ValueError`` for an unknown slot."""
    catalog = _pick_slots(control.known_policy_slots(), slot)
    if as_json:
        _dump(catalog)
        return 0
    tbl = Table(title="Policy slots" if slot is None else f"Policies · {slot}", header_style="bold cyan")
    tbl.add_column("Slot", no_wrap=True)
    tbl.add_column("Policy", min_width=30)
    tbl.add_column("Default", justify="center", no_wrap=True)
    tbl.add_column("Status", no_wrap=True)
    for name, entry in catalog.items():
        if entry.get("status") == "implemented":
            _implemented_rows(tbl, name, entry)
        else:
            _muted_rows(tbl, name, entry)
    console.print(tbl)
    console.print(
        "[dim]Select per run with[/] [bold]openride run <slug> --policy dispatch=<name>[/] "
        "[dim](params: --policy-param dispatch.<key>=<value>); details:[/] "
        "[bold]openride policy show <slot> <name>[/]"
    )
    return 0


def _find_policy(catalog: dict[str, Any], slot: str, name: str) -> dict[str, Any]:
    entry = _pick_slots(catalog, slot)[slot]
    if entry.get("status") != "implemented":
        raise ValueError(f"slot {slot!r} is {entry.get('status')}; it has no registered policies to show")
    for p in entry.get("policies") or []:
        if p.get("name") == name:
            return {**p, "slot": slot, "is_default": name == entry.get("default")}
    names = ", ".join(p.get("name", "?") for p in entry.get("policies") or [])
    raise ValueError(f"unknown {slot} policy {name!r}; registered: {names}")


def _bounds(prop: dict[str, Any]) -> str:
    parts = [f"{op} {prop[key]}" for key, op in _BOUND_KEYS if key in prop]
    if "enum" in prop:
        parts.append("one of " + ", ".join(str(v) for v in prop["enum"]))
    return "; ".join(parts)


def _invalid_as_bad_input(exc: control.ControlError) -> Exception:
    # A refusal of the INPUT (unknown slot/name, bad params) is exit 2 like every other
    # bad argument (cli.py); a battery FAIL stays exit 1.
    return ValueError(str(exc)) if exc.code == "INVALID" else exc


def _cmd_list(args) -> int:
    return render_policy_list(args.slot, as_json=args.json)


def _cmd_show(args) -> int:
    info = _find_policy(control.known_policy_slots(), args.slot, args.name)
    if args.json:
        _dump(info)
        return 0
    lines = [
        f"[bold]{info['name']}[/]  [dim]{info['slot']} · version {info.get('version')}"
        + (" · slot default" if info["is_default"] else "") + "[/]",
        escape(str(info.get("description") or "")),
    ]
    if info.get("requires"):
        missing = info.get("missing") or []
        lines.append(f"requires: {', '.join(info['requires'])}"
                     + (f"  [red](missing: {', '.join(missing)})[/]" if missing else ""))
    if info.get("order_sensitive"):
        lines.append(f"[yellow]order-sensitive:[/] {escape(str(info['order_sensitive']))}")
    console.print(Panel("\n".join(lines), border_style="cyan", title="policy"))
    props = (info.get("params_schema") or {}).get("properties") or {}
    if not props:
        console.print("[dim]No params.[/]")
        return 0
    tbl = Table(title="Params", header_style="bold magenta")
    for col in ("Param", "Type", "Default"):
        tbl.add_column(col, no_wrap=True)
    tbl.add_column("Bounds", max_width=18)
    tbl.add_column("Help", min_width=24)
    for key, prop in props.items():
        tbl.add_row(f"[bold]{key}[/]", str(prop.get("type", "")), json.dumps(prop.get("default")),
                    escape(_bounds(prop)), escape(str(prop.get("description") or "")))
    console.print(tbl)
    return 0


def _cmd_validate(args) -> int:
    try:
        payload = control.policy_validate(args.slot, args.name)
    except control.ControlError as exc:
        raise _invalid_as_bad_input(exc) from exc
    tbl = Table(title=f"Contract battery · {args.slot} {args.name}", header_style="bold cyan")
    tbl.add_column("Check", no_wrap=True)
    tbl.add_column("Result", no_wrap=True)
    tbl.add_column("ms", justify="right", no_wrap=True)
    tbl.add_column("Detail")
    for r in payload.get("results") or []:
        verdict = "[green]PASS[/]" if r.get("ok") else "[bold red]FAIL[/]"
        tbl.add_row(str(r.get("id")), verdict, f"{float(r.get('ms') or 0):.1f}", escape(str(r.get("detail") or "")))
    console.print(tbl)
    ok = bool(payload.get("ok"))
    style = "bold green" if ok else "bold red"
    console.print(f"[{style}]{'PASS' if ok else 'FAIL'}[/] {escape(str(payload.get('message', '')))}")
    return 0 if ok else 1


def _cmd_bench(args) -> int:
    names = [n.strip() for n in args.policies.split(",") if n.strip()]
    try:
        result = control.policy_bench(args.slot, names, fixtures_dir=args.fixtures,
                                      synthetic=args.synthetic, params=args.policy_param)
    except control.ControlError as exc:
        raise _invalid_as_bad_input(exc) from exc
    if args.json:
        _dump(result["report"])
        return 0
    # The control plane's fixed-width table (bench.render_table): one renderer for the
    # CLI and the control payload, and it never squeezes columns on a narrow terminal.
    console.print(result["table"], markup=False, highlight=False, soft_wrap=True)
    violations = sum(int(r.get("violations") or 0) for r in result["report"].get("rows") or [])
    if violations:
        console.print(f"[bold red]{violations} plan(s) violated the dispatch contract and were discarded.[/]")
    return 0


def register(sub) -> None:
    pol = sub.add_parser("policy", help="Pluggable policies: list / show / validate / bench")
    psub = pol.add_subparsers(dest="policy_cmd", required=True)

    ls = psub.add_parser("list", help="List policy slots and their policies")
    ls.add_argument("slot", nargs="?", default=None, help="Only this slot (e.g. dispatch)")
    ls.add_argument("--json", action="store_true", help="Emit the raw catalog JSON")
    ls.set_defaults(func=_cmd_list)

    show = psub.add_parser("show", help="Describe one policy and its params")
    show.add_argument("slot")
    show.add_argument("name")
    show.add_argument("--json", action="store_true", help="Emit the raw catalog entry")
    show.set_defaults(func=_cmd_show)

    val = psub.add_parser("validate", help="Run the contract battery (exit 1 on any FAIL)")
    val.add_argument("slot")
    val.add_argument("name")
    val.set_defaults(func=_cmd_validate)

    bench = psub.add_parser("bench", help="Single-tick bench on synthetic or captured fixtures")
    bench.add_argument("slot")
    bench.add_argument("--policies", required=True, metavar="A,B", help="Comma-separated policy names")
    src = bench.add_mutually_exclusive_group(required=True)
    src.add_argument("--fixtures", default=None, metavar="DIR",
                     help="Captured fixtures (openride run --capture-dispatch)")
    src.add_argument("--synthetic", default=None, metavar="NAME[,NAME]|all",
                     help="Synthetic fixture(s), e.g. scale")
    bench.add_argument("--policy-param", dest="policy_param", action="append", default=[],
                       metavar="POLICY.KEY=VALUE",
                       help="Param override for one benched policy, repeatable; VALUE is JSON when it parses")
    bench.add_argument("--json", action="store_true", help="Emit the raw report JSON")
    bench.set_defaults(func=_cmd_bench)
