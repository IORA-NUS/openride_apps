"""``openride policy ...`` — list, inspect, validate and bench pluggable policies.

Single responsibility: render the control plane's policy surface (``known-policies``
``slots`` and ``components``, ``policy-validate``, ``policy-bench``) in the terminal.
Every name, schema and default comes from the engine's catalog through
:mod:`openride.control`; nothing here hand-copies a policy list
(``tests/test_cli_no_hardcoded_policy_lists.py``). ``openride solver list`` is
``policy list dispatch`` restricted to the per-scope solvers (leaves;
:func:`render_policy_list`). Authoring guide: ``docs/policy_authoring.md``.
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


def _pick(catalog: dict[str, Any], components: dict[str, Any], name: str | None) -> tuple[dict, dict]:
    """``(slots, component kinds)`` to render: everything, one slot, or one component kind."""
    if name is None:
        return catalog, components
    if name in catalog:
        return {name: catalog[name]}, {}
    if name in components:
        return {}, {name: components[name]}
    raise ValueError(
        f"unknown policy slot or component kind {name!r}; slots: {', '.join(catalog)}"
        + (f"; component kinds: {', '.join(components)}" if components else "")
    )


def _model(p: dict[str, Any]) -> str:
    """The information-model cell: the declared model, plus ``composite (inner)`` for a decomposition."""
    model = str(p.get("information_model") or "—")
    if p.get("composite"):
        model += f"\n[dim]composite ({p.get('inner_param') or '?'})[/]"
    return model


def _implemented_rows(tbl: Table, slot: str, entry: dict[str, Any], *, leaves_only: bool) -> None:
    for p in entry.get("policies") or []:
        if leaves_only and p.get("composite"):
            continue
        name = p.get("name", "?")
        default = "✓" if name == entry.get("default") else ""
        missing = p.get("missing") or []
        if p.get("error"):
            status = f"[red]broken: {escape(str(p['error']))}[/]"
        else:
            status = f"[red]missing: {', '.join(missing)}[/]" if missing else "[green]available[/]"
        cell = f"[bold]{name}[/] v{p.get('version', '?')}\n[dim]{escape(str(p.get('description') or ''))}[/]"
        tbl.add_row(slot, cell, _model(p), default, status)


def _muted_rows(tbl: Table, slot: str, entry: dict[str, Any]) -> None:
    status = entry.get("status")
    note = f"{entry.get('kind', '')} slot, lands in {entry.get('lands_in', '?')}"
    tbl.add_row(f"[dim]{slot}[/]", f"[dim]— {note}[/]", "", "", f"[dim]{status}[/]")


def _component_rows(tbl: Table, kind: str, entry: dict[str, Any]) -> None:
    for p in entry.get("policies") or []:
        name = p.get("name", "?")
        default = "✓" if name == entry.get("default") else ""
        cell = f"[bold]{name}[/] v{p.get('version', '?')}\n[dim]{escape(str(p.get('description') or ''))}[/]"
        status = f"[red]broken: {escape(str(p['error']))}[/]" if p.get("error") else "[green]available[/]"
        tbl.add_row(kind, cell, "[dim]component[/]", default, status)


def render_policy_list(name: str | None = None, *, as_json: bool = False, leaves_only: bool = False) -> int:
    """Print the catalog: every slot and component kind, one slot, or one component kind.

    ``leaves_only`` keeps only the dispatch policies that are not decompositions (the
    per-scope solvers ``--solver`` accepts; ``openride solver list``). Component kinds
    are fetched only when listing everything or naming a kind. Raises ``ValueError``
    for an unknown name.
    """
    catalog = control.known_policy_slots()
    components = control.known_policy_components() if (name is None or name not in catalog) else {}
    slots, kinds = _pick(catalog, components, name)
    if leaves_only:
        slots = {k: {**v, "policies": [p for p in v.get("policies") or [] if not p.get("composite")]}
                 for k, v in slots.items()}
    if as_json:
        _dump({**slots, **({"components": kinds} if kinds else {})})
        return 0
    title = "Policy slots" if name is None else (f"Solvers · {name}" if leaves_only else f"Policies · {name}")
    tbl = Table(title=title, header_style="bold cyan")
    tbl.add_column("Slot / kind", no_wrap=True)
    tbl.add_column("Policy", min_width=30)
    tbl.add_column("Info model", no_wrap=True)
    tbl.add_column("Default", justify="center", no_wrap=True)
    tbl.add_column("Status", no_wrap=True)
    for slot, entry in slots.items():
        if entry.get("status") == "implemented":
            _implemented_rows(tbl, slot, entry, leaves_only=leaves_only)
        else:
            _muted_rows(tbl, slot, entry)
    for kind, entry in kinds.items():
        _component_rows(tbl, kind, entry)
    console.print(tbl)
    console.print(
        "[dim]Select per run with[/] [bold]openride run <slug> --policy dispatch=<name>[/] "
        "[dim](nested params: --policy-param dispatch.inner.<key>=<value>; the per-scope solver:[/] "
        "[bold]--solver <name>[/][dim]); details:[/] [bold]openride policy show <slot|kind> <name>[/]"
    )
    return 0


def _find_policy(catalog: dict[str, Any], components: dict[str, Any], slot: str, name: str) -> dict[str, Any]:
    if slot in components:
        entry = components[slot]
        kind = "component"
    else:
        entry = _pick(catalog, components, slot)[0][slot]
        kind = "slot"
        if entry.get("status") != "implemented":
            raise ValueError(f"slot {slot!r} is {entry.get('status')}; it has no registered policies to show")
    for p in entry.get("policies") or []:
        if p.get("name") == name:
            return {**p, "slot": slot, "is_default": name == entry.get("default"), "entry_kind": kind}
    names = ", ".join(p.get("name", "?") for p in entry.get("policies") or [])
    raise ValueError(f"unknown {slot} policy {name!r}; registered: {names}")


def _bounds(prop: dict[str, Any]) -> str:
    parts = [f"{op} {prop[key]}" for key, op in _BOUND_KEYS if key in prop]
    if "enum" in prop:
        parts.append("one of " + ", ".join(str(v) for v in prop["enum"]))
    return "; ".join(parts)


def _ref_targets(kind: str, catalog: dict[str, Any], components: dict[str, Any], leaf_only: bool) -> list[str]:
    """The names a ref param of ``kind`` accepts (dispatch: the leaves when ``leaf_only``)."""
    entry = catalog.get(kind) if kind in catalog else components.get(kind) or {}
    return [p.get("name", "?") for p in entry.get("policies") or []
            if not (leaf_only and p.get("composite"))]


def _type_cell(prop: dict[str, Any]) -> str:
    kind = prop.get("x-policy-ref")
    if kind:
        return f"ref → {kind}" + (" (leaf)" if prop.get("x-leaf-only") else "")
    t = prop.get("type", "")
    return " | ".join(t) if isinstance(t, list) else str(t)


def _default_cell(prop: dict[str, Any]) -> str:
    default = prop.get("default")
    if prop.get("x-policy-ref") and isinstance(default, dict):
        params = default.get("params") or {}
        return str(default.get("type")) + (f" {json.dumps(params, sort_keys=True)}" if params else "")
    return json.dumps(default)


def _invalid_as_bad_input(exc: control.ControlError) -> Exception:
    # A refusal of the INPUT (unknown slot/name, bad params) is exit 2 like every other
    # bad argument (cli.py); a battery FAIL stays exit 1.
    return ValueError(str(exc)) if exc.code == "INVALID" else exc


def _cmd_list(args) -> int:
    return render_policy_list(args.slot, as_json=args.json)


def _cmd_show(args) -> int:
    catalog = control.known_policy_slots()
    components = control.known_policy_components() if args.slot not in catalog else {}
    info = _find_policy(catalog, components, args.slot, args.name)
    if args.json:
        _dump(info)
        return 0
    head = f"{info['slot']} · version {info.get('version')}" + (
        " · slot default" if info["is_default"] and info["entry_kind"] == "slot" else
        " · kind default" if info["is_default"] else "")
    lines = [f"[bold]{info['name']}[/]  [dim]{head}[/]", escape(str(info.get("description") or ""))]
    if info.get("information_model"):
        lines.append(f"information model: [bold]{info['information_model']}[/]"
                     + (f"  · composite (decomposes the tick; per-scope solver in '{info.get('inner_param')}')"
                        if info.get("composite") else "  · leaf (a per-scope solver; --solver accepts it)"))
    if info.get("share_tag_format"):
        lines.append(f"share tag format: {info['share_tag_format']}")
    if info.get("requires"):
        missing = info.get("missing") or []
        lines.append(f"requires: {', '.join(info['requires'])}"
                     + (f"  [red](missing: {', '.join(missing)})[/]" if missing else ""))
    if info.get("order_sensitive"):
        lines.append(f"[yellow]order-sensitive:[/] {escape(str(info['order_sensitive']))}")
    if info.get("module_rng"):
        lines.append(f"[yellow]module RNG:[/] {escape(str(info['module_rng']))}")
    if info.get("error"):
        lines.append(f"[red]broken:[/] {escape(str(info['error']))}")
    console.print(Panel("\n".join(lines), border_style="cyan", title="policy"))
    props = (info.get("params_schema") or {}).get("properties") or {}
    if not props:
        console.print("[dim]No params.[/]")
        return 0
    tbl = Table(title="Params", header_style="bold magenta")
    for col in ("Param", "Type", "Default"):
        tbl.add_column(col, no_wrap=True)
    tbl.add_column("Bounds / options", max_width=36)
    tbl.add_column("Help", min_width=24)
    refs = []
    for key, prop in props.items():
        bounds = _bounds(prop)
        if prop.get("x-policy-ref"):
            if not components and prop["x-policy-ref"] not in catalog:
                components = control.known_policy_components()
            names = _ref_targets(prop["x-policy-ref"], catalog, components, bool(prop.get("x-leaf-only")))
            bounds = "one of " + ", ".join(names)
            refs.append(key)
        tbl.add_row(f"[bold]{key}[/]", _type_cell(prop), _default_cell(prop),
                    escape(bounds), escape(str(prop.get("description") or "")))
    console.print(tbl)
    if refs:
        slot = info["slot"]
        console.print(
            f"[dim]A ref param ({', '.join(refs)}) selects another policy and nests its params: "
            f"--policy-param {slot}.<ref>=<name>, --policy-param {slot}.<ref>.<key>=<value>; "
            "details: openride policy show <kind> <name>[/]"
        )
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

    ls = psub.add_parser("list", help="List policy slots, their policies and the market components")
    ls.add_argument("slot", nargs="?", default=None, metavar="SLOT|KIND",
                    help="Only this slot (e.g. dispatch) or component kind (e.g. market.offer)")
    ls.add_argument("--json", action="store_true", help="Emit the raw catalog JSON")
    ls.set_defaults(func=_cmd_list)

    show = psub.add_parser("show", help="Describe one policy (or market component) and its params")
    show.add_argument("slot", metavar="SLOT|KIND")
    show.add_argument("name")
    show.add_argument("--json", action="store_true", help="Emit the raw catalog entry")
    show.set_defaults(func=_cmd_show)

    val = psub.add_parser("validate", help="Run the contract battery (exit 1 on any FAIL)")
    val.add_argument("slot")
    val.add_argument("name")
    val.set_defaults(func=_cmd_validate)

    bench = psub.add_parser("bench", help="Whole-tick bench on synthetic or captured fixtures")
    bench.add_argument("slot")
    bench.add_argument("--policies", required=True, metavar="A,ALIAS=B",
                       help="Comma-separated NAME or ALIAS=NAME entries (one policy benched in several "
                            "configurations: pm=PooledMarket,pr=PooledMarket)")
    src = bench.add_mutually_exclusive_group(required=True)
    src.add_argument("--fixtures", default=None, metavar="DIR",
                     help="Captured fixtures (openride run --capture-dispatch)")
    src.add_argument("--synthetic", default=None, metavar="NAME[,NAME]|all",
                     help="Synthetic fixture(s), e.g. scale")
    bench.add_argument("--policy-param", dest="policy_param", action="append", default=[],
                       metavar="ALIAS.PATH=VALUE",
                       help="Param override for one benched entry, repeatable; nested like --policy-param "
                            "(pr.inner=RandomAssignment, pr.inner.candidates=spatial); VALUE is JSON when it parses")
    bench.add_argument("--json", action="store_true", help="Emit the raw report JSON")
    bench.set_defaults(func=_cmd_bench)
