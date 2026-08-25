"""``openride scenario ...`` — construct and maintain scenarios from the terminal.

Every primitive is the same one the dashboard uses (we shell out to
``openride_control.command``); this just gives it a human face and lets you author a
scenario entirely from flags / files instead of the browser:

    openride scenario list
    openride scenario show <slug>
    openride scenario new  --name "Demo" --trucks 200 --orders 2000 --days 7 \
                           --haulier "Patrick Inc:60:60" --haulier "Global:40:40" \
                           --gen-file my_override.py --input demand.xlsx
    openride scenario new  --spec-file spec.json
    openride scenario edit <slug> --set simulationDays=10 --solver GreedyNearest
    openride scenario edit <slug>            # opens spec.json in $EDITOR
    openride scenario compile <slug>
    openride scenario rules-baseline <slug>   # re-record the facility world facilityRules target
    openride scenario delete <slug>
    openride scenario reindex
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from typing import Any, Optional

from rich.panel import Panel
from rich.table import Table

from . import config, control
from .ui import console, render_scenario_detail, render_scenarios


# -- spec authoring helpers ----------------------------------------------


def _parse_haulier(raw: str) -> dict[str, Any]:
    """``"Name:fleet%:order%"`` -> ``{name, fleet_share, order_share}``.

    Accepts ``Name`` (shares left unset -> equal split), ``Name:fleet`` (order = fleet),
    or ``Name:fleet:order``.
    """
    parts = raw.split(":")
    name = parts[0].strip()
    if not name:
        raise ValueError(f"Bad --haulier {raw!r}: name is empty")
    if len(parts) > 3:
        raise ValueError(f"Bad --haulier {raw!r}: expected 'Name', 'Name:fleet', or 'Name:fleet:order'")

    def _share(token: str, which: str) -> float:
        try:
            return float(token)
        except ValueError:
            raise ValueError(f"Bad --haulier {raw!r}: {which} share {token!r} is not a number") from None

    out: dict[str, Any] = {"name": name}
    if len(parts) >= 2 and parts[1].strip():
        out["fleet_share"] = _share(parts[1], "fleet")
        out["order_share"] = out["fleet_share"]
    if len(parts) >= 3 and parts[2].strip():
        out["order_share"] = _share(parts[2], "order")
    return out


def _load_data_file(path: str) -> Any:
    if not os.path.isfile(path):
        raise ValueError(f"file not found: {path}")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path} is not valid JSON: {exc}") from None


def _coerce(value: str) -> Any:
    """Coerce a ``--set key=value`` string: JSON if it parses, else the raw string."""
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return value


def _spec_from_flags(args) -> dict[str, Any]:
    """Build the subset of recipe fields the user set via convenience flags.

    Only keys the user actually passed are included, so this can be merged over a
    ``--spec-file`` (new) or used as an edit patch without clobbering untouched fields.
    """
    spec: dict[str, Any] = {}
    if getattr(args, "name", None):
        spec["name"] = args.name
    if getattr(args, "slug", None):
        spec["slug"] = args.slug
    if getattr(args, "days", None) is not None:
        spec["simulationDays"] = args.days

    agents: dict[str, Any] = {}
    if getattr(args, "trucks", None) is not None:
        agents.setdefault("truck", {})["count"] = args.trucks
    if getattr(args, "orders", None) is not None:
        agents.setdefault("order", {})["count"] = args.orders
    if getattr(args, "facilities", None) is not None:
        agents.setdefault("facility", {})["count"] = args.facilities

    # Per-role policy selection. Builds agents.<role>.policy = {type, ...params}.
    if getattr(args, "truck_policy", None):
        agents.setdefault("truck", {})["policy"] = {"type": args.truck_policy}
    if getattr(args, "facility_policy", None):
        agents.setdefault("facility", {})["policy"] = {"type": args.facility_policy}
    if getattr(args, "order_policy", None) or getattr(args, "order_source", None):
        order_policy: dict[str, Any] = {"type": getattr(args, "order_policy", None) or "matrix"}
        if getattr(args, "trip_matrix_file", None):
            order_policy["matrix"] = {"$file": os.path.abspath(args.trip_matrix_file)}
        if getattr(args, "demand_curve_file", None):
            order_policy["curve"] = {"$file": os.path.abspath(args.demand_curve_file)}
        if getattr(args, "order_source", None):
            order_policy["source"] = os.path.abspath(args.order_source)
        agents.setdefault("order", {})["policy"] = order_policy
    if agents:
        spec["agents"] = agents

    if getattr(args, "seed", None) is not None:
        spec["seed"] = args.seed
    if getattr(args, "order_unit", None):
        spec["orderCountUnit"] = args.order_unit
    if getattr(args, "early_orders", None) is not None:
        spec["earlyOrderCount"] = args.early_orders
    if getattr(args, "solver", None):
        spec["solver"] = args.solver
    if getattr(args, "haulier", None):
        spec["hauliers"] = [_parse_haulier(h) for h in args.haulier]
    # Matrix / curve files (JSON, CSV, or .xlsx) are parsed server-side at spec creation.
    # When an --order-policy is given the files ride inside agents.order.policy (above);
    # otherwise keep the legacy top-level fields (the Preprocessor reads both).
    order_has_policy = bool(getattr(args, "order_policy", None) or getattr(args, "order_source", None))
    if not order_has_policy:
        if getattr(args, "demand_curve_file", None):
            spec["orderDemandCurve"] = {"$file": os.path.abspath(args.demand_curve_file)}
        if getattr(args, "trip_matrix_file", None):
            spec["tripMatrix"] = {"$file": os.path.abspath(args.trip_matrix_file)}

    for item in getattr(args, "set", None) or []:
        if "=" not in item:
            raise ValueError(f"Bad --set {item!r}: expected key=value")
        key, value = item.split("=", 1)
        spec[key.strip()] = _coerce(value)
    return spec


def _deep_merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _editor_patch(current: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Open the current recipe in ``$EDITOR``; return the edited dict (or None if unchanged)."""
    editor = os.environ.get("EDITOR") or os.environ.get("VISUAL") or "nano"
    with tempfile.NamedTemporaryFile("w", suffix=".spec.json", delete=False, encoding="utf-8") as fh:
        json.dump(current, fh, indent=2, sort_keys=True)
        path = fh.name
    before = json.dumps(current, sort_keys=True)
    try:
        subprocess.run([*editor.split(), path], check=True)
        with open(path, "r", encoding="utf-8") as fh:
            edited = json.load(fh)
    finally:
        os.unlink(path)
    if json.dumps(edited, sort_keys=True) == before:
        return None
    return edited


# -- handlers -------------------------------------------------------------


def _cmd_list(args) -> int:
    scenarios = control.list_scenarios(args.domain)
    if args.json:
        json.dump(scenarios, sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        render_scenarios(scenarios)
    return 0


def _cmd_show(args) -> int:
    detail = control.get_scenario(args.slug, args.domain)
    if args.json:
        json.dump(detail, sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        render_scenario_detail(detail)
    return 0


def _check_source_paths(args) -> None:
    """Validate --gen-file / --input exist *before* we generate, so a bad path can't
    leave a freshly-created scenario without the sources the user intended."""
    if getattr(args, "gen_file", None) and not os.path.isfile(args.gen_file):
        raise ValueError(f"--gen-file not found: {args.gen_file}")
    for path in getattr(args, "input", None) or []:
        if not os.path.isfile(path):
            raise ValueError(f"--input not found: {path}")


def _maybe_prompt_for_input_files(spec: dict[str, Any]) -> None:
    """TUI option: when interactive and not already supplied, offer to load the trip
    matrix / demand curve from a JSON/CSV/Excel file. Blank keeps the template default.
    Sets a ``{"$file": …}`` ref the server parses + inlines at spec creation."""
    if not sys.stdin.isatty():
        return
    import questionary

    if "tripMatrix" not in spec:
        path = questionary.path("Trip matrix file (JSON/CSV/Excel — blank = template default):").ask()
        if path and path.strip():
            spec["tripMatrix"] = {"$file": os.path.abspath(path.strip())}
    if "orderDemandCurve" not in spec:
        path = questionary.path("Demand-curve file (JSON/CSV/Excel — blank = default):").ask()
        if path and path.strip():
            spec["orderDemandCurve"] = {"$file": os.path.abspath(path.strip())}


def _cmd_new(args) -> int:
    _check_source_paths(args)
    interactive = sys.stdin.isatty() and sys.stdout.isatty()

    # Bare `scenario new` on a TTY (no recipe given) → guided wizard, seeded by any flags.
    if interactive and not args.spec_file and not args.name:
        return interactive_new(args.domain, seed=_spec_from_flags(args))

    spec: dict[str, Any] = {}
    if args.spec_file:
        spec = _load_data_file(args.spec_file)
        if not isinstance(spec, dict):
            console.print("[red]--spec-file must contain a JSON object.[/]")
            return 2
    spec = _deep_merge(spec, _spec_from_flags(args))
    _maybe_prompt_for_input_files(spec)
    if not spec.get("name"):
        console.print("[red]A scenario needs a --name (or a name in --spec-file).[/]")
        return 2
    # Match the dashboard: an order count is a per-day rate unless the user says otherwise.
    spec.setdefault("orderCountUnit", "per_day")
    _create_scenario(
        spec, gen_file=args.gen_file, inputs=args.input, overwrite=args.overwrite,
        domain=args.domain,
        allow_cooperation_reset=getattr(args, "allow_cooperation_reset", False),
    )
    return 0


def _cmd_edit(args) -> int:
    _check_source_paths(args)
    interactive = sys.stdin.isatty() and sys.stdout.isatty()

    # Bare `scenario edit <slug>` on a TTY (no patch given) → guided/EDITOR chooser.
    if interactive and not args.spec_file and not args.set and not _has_authoring_flags(args) \
            and not (args.gen_file or args.input):
        return interactive_edit(args.domain, slug=args.slug)

    flag_patch = _spec_from_flags(args)
    file_patch: dict[str, Any] = {}
    if args.spec_file:
        loaded = _load_data_file(args.spec_file)
        if not isinstance(loaded, dict):
            console.print("[red]--spec-file must contain a JSON object.[/]")
            return 2
        file_patch = loaded
    patch = _deep_merge(file_patch, flag_patch)

    # No explicit edits + a TTY -> open the current recipe (the faithful spec.json) in $EDITOR.
    if not patch and sys.stdin.isatty() and sys.stdout.isatty() and not (args.gen_file or args.input):
        current = control.get_spec(args.slug, args.domain)
        edited = _editor_patch(current)
        if edited is None:
            console.print("[dim]No changes.[/]")
            return 0
        patch = edited

    if patch:
        with console.status(f"[cyan]Recompiling [bold]{args.slug}[/]…", spinner="dots"):
            control.edit_scenario(
                args.slug, patch, domain=args.domain,
                allow_cooperation_reset=getattr(args, "allow_cooperation_reset", False),
            )
        console.print(f"[green]Edited[/] [bold]{args.slug}[/].")

    if args.gen_file or args.input:
        with console.status("[cyan]Staging sources + recompiling…", spinner="dots"):
            control.stage_sources(args.slug, gen_file=args.gen_file, inputs=args.input, domain=args.domain)
        console.print("[green]Staged sources and recompiled.[/]")

    render_scenario_detail(control.get_scenario(args.slug, args.domain))
    return 0


def _cmd_compile(args) -> int:
    if getattr(args, "baseline_rules", False):
        # Re-record the world FIRST, then compile — the create-then-compile flow.
        # A non-zero rc is a refusal or an abort, and must not fall through to a
        # compile the operator did not approve.
        rc = _rules_baseline(
            args.slug, args.domain, yes=bool(getattr(args, "yes", False)),
            # A rules-less scenario is the COMMON case here and is not a reason to
            # refuse a compile the operator asked for.
            no_rules_is_error=False,
        )
        if rc:
            return rc
    reseed = bool(getattr(args, "reseed", False))
    label = "Regenerating (new data)" if reseed else "Compiling"
    with console.status(f"[cyan]{label} [bold]{args.slug}[/]…", spinner="dots"):
        control.compile_scenario(args.slug, args.domain, reseed=reseed)
    console.print(f"[green]{'Regenerated' if reseed else 'Compiled'}[/] [bold]{args.slug}[/].")
    render_scenario_detail(control.get_scenario(args.slug, args.domain))
    return 0


# -- facilityRules re-baseline -------------------------------------------
#
# `facilityRules` are only meaningful against the facility world they were written
# for: names are GENERATED and positional, so a rule can still match after a
# regeneration while addressing a different physical site. `Preprocessor.compile`
# therefore refuses to compile when the recorded `facilityRulesWorld` digest no
# longer matches. This is the only sanctioned way past that refusal, and it is a
# deliberate human act: it re-records the world and prints what each rule now
# addresses. It VALIDATES NOTHING about intent — see the caveat panel below, which
# is the point of the command, not boilerplate.


def _fmt_count(value: Any) -> str:
    return "—" if value is None else str(value)


def _render_world_table(recorded: Optional[dict[str, Any]], now: dict[str, Any]) -> None:
    """Recorded vs now, side by side: counts per code first, digest last."""
    rec = recorded or {}
    rec_codes = rec.get("code_counts") if isinstance(rec.get("code_counts"), dict) else {}
    now_codes = now.get("code_counts") or {}

    tbl = Table(title="Facility world", header_style="bold magenta", title_style="bold")
    tbl.add_column("")
    # fold, never truncate: a digest shortened to an ellipsis is unusable for the
    # one thing it is here for — telling two worlds apart.
    tbl.add_column("recorded", justify="right", overflow="fold")
    tbl.add_column("now", justify="right", overflow="fold")
    tbl.add_column("")

    def _row(label: str, before: Any, after: Any, *, numeric: bool = True) -> None:
        same = before == after
        mark = "" if same else ("[yellow]changed[/]" if before is not None else "[cyan]new[/]")
        style = "" if same else "yellow"
        left, right = _fmt_count(before), _fmt_count(after)
        if not numeric:
            left, right = str(before or "—"), str(after or "—")
        tbl.add_row(label, left, f"[{style}]{right}[/]" if style else right, mark)

    _row("facilities", rec.get("facility_count"), now.get("facility_count"))
    for code in sorted(set(rec_codes) | set(now_codes)):
        _row(f"  code {code}", rec_codes.get(code), now_codes.get(code))
    _row("site_digest", rec.get("site_digest"), now.get("site_digest"), numeric=False)
    console.print(tbl)


def _render_rules_table(report: dict[str, Any]) -> None:
    """Every rule and what it addresses in the NEW world.

    This table is why the command exists: it is the only place a human sees that
    ``match.code: "CT"`` went from 6 facilities to 8.
    """
    rec = report.get("recorded") or {}
    rec_codes = rec.get("code_counts") if isinstance(rec.get("code_counts"), dict) else {}

    tbl = Table(
        title="Rules under the NEW world", header_style="bold magenta", title_style="bold"
    )
    tbl.add_column("rule")
    tbl.add_column("sets")
    tbl.add_column("matched (was)", justify="right")
    tbl.add_column("facilities")

    for entry in report.get("rules") or []:
        if entry.get("error"):
            tbl.add_row(entry.get("label", "?"), "", "[red]?[/]", f"[red]{entry['error']}[/]")
            continue
        matched = entry.get("matched")
        # "was" is only knowable for a `code` matcher — the recorded block stores
        # per-code counts, never per-name ones.
        was = rec_codes.get(entry.get("matcher_value")) if entry.get("matcher") == "code" else None
        if matched == 0:
            count = "[red]0[/]"
        elif was is not None and was != matched:
            count = f"[yellow]{matched}[/] (was {was})"
        else:
            count = f"{matched}" + (f" (was {was})" if was is not None else "")
        names = ", ".join(entry.get("examples") or []) or "[red]none[/]"
        if matched and matched > len(entry.get("examples") or []):
            names += ", …"
        tbl.add_row(entry.get("label", "?"), ", ".join(entry.get("set_keys") or []), count, names)
    console.print(tbl)


def _rules_baseline(
    slug: str, domain: Optional[str], *, yes: bool, no_rules_is_error: bool = True
) -> int:
    """Re-record ``facilityRulesWorld`` for ``slug``. 0 = done, 1 = aborted, 2 = refused.

    ``no_rules_is_error`` is False when this rides ``compile --baseline-rules``: a
    rules-less scenario is the common case there (13 of the 15 shipped author no
    rules) and is a notice, not a refusal.
    """
    with console.status(f"[cyan]Generating the facility world for [bold]{slug}[/]…", spinner="dots"):
        report = control.facility_rules_baseline(slug, domain)

    if not report.get("has_rules"):
        # NOT an error when it rides `compile --baseline-rules` (R2-8 / review F6).
        # 13 of the 15 shipped scenarios author no rules, so turning rc 0 into rc 2 on
        # the common case trains operators to drop the flag — worse than not having
        # it. Nothing failed: there was simply nothing to re-baseline. Non-zero stays
        # reserved for a genuine refusal (`--yes` withheld, an abort, a stale world).
        if no_rules_is_error:
            console.print(
                f"[red]{slug} authors no [bold]facilityRules[/], so there is no baseline "
                "to record.[/]\nThe world block exists to bind a rule list to the world "
                "it was written against; recording one with no rules would bless a world "
                "nobody reviewed — which is exactly what the guard prevents. Author the "
                "rules first."
            )
            return 2
        console.print(
            f"[dim]{slug} authors no facilityRules — nothing to re-baseline; "
            "continuing with the compile.[/]"
        )
        return 0

    recorded, now = report.get("recorded"), report.get("now") or {}
    _render_world_table(recorded, now)
    _render_rules_table(report)

    if report.get("validation_error"):
        console.print(
            Panel(
                str(report["validation_error"]),
                border_style="red",
                title="these rules will NOT compile against the new world",
            )
        )

    console.print(
        Panel(
            "This records the facility world as it is [bold]now[/] and nothing else.\n"
            "It does [bold]NOT[/] check that your rules still mean what you intended: "
            "facility names are generated and positional, so a rule that still matches "
            "may now address a [bold]different physical site[/]. The match counts above "
            "are the review — there is no other one.\n"
            "[dim]Only facilityRulesWorld is rewritten; the rules themselves are never "
            "touched, and the scenario is not recompiled.[/]",
            border_style="yellow",
            title="what re-baselining does (and does not) do",
        )
    )

    if recorded and recorded.get("site_digest") == now.get("site_digest"):
        console.print(
            "[dim]The recorded world already matches the generated one — nothing to "
            "move forward. spec.json left untouched.[/]"
        )
        return 0

    if not yes:
        if not sys.stdin.isatty():
            console.print(
                f"[red]Refusing to re-baseline[/] [bold]{slug}[/] non-interactively "
                "without [bold]--yes[/]."
            )
            return 2
        import questionary

        if not questionary.confirm(
            f"Record this world as the baseline for {slug}'s facilityRules?", default=False
        ).ask():
            console.print("[dim]Aborted — spec.json untouched.[/]")
            return 1

    control.write_facility_rules_world(report["scenario_path"], report["snapshot"])
    console.print(
        f"[green]Recorded[/] the new facility world in [bold]{slug}[/]'s spec.json "
        f"([dim]{report['snapshot'].get('site_digest')}[/]).\n"
        f"[dim]Rules unchanged, scenario not recompiled — run "
        f"`openride scenario compile {slug}` when the rules are what you want.[/]",
        highlight=False,  # rich would colourise the digest as a number
    )
    return 0


def _cmd_rules_baseline(args) -> int:
    return _rules_baseline(args.slug, args.domain, yes=bool(args.yes))


def _cmd_delete(args) -> int:
    # Delete is irreversible. Require an interactive yes, or an explicit --yes when scripted —
    # never delete silently just because there's no TTY to prompt on.
    if not args.yes:
        if not sys.stdin.isatty():
            console.print(
                f"[red]Refusing to delete[/] [bold]{args.slug}[/] non-interactively without "
                "[bold]--yes[/]."
            )
            return 2
        import questionary

        if not questionary.confirm(f"Delete scenario {args.slug}?", default=False).ask():
            console.print("[dim]Aborted.[/]")
            return 0
    control.delete_scenario(args.slug, args.domain)
    console.print(f"[green]Deleted[/] [bold]{args.slug}[/].")
    return 0


def _cmd_reindex(args) -> int:
    result = control.rebuild_index(args.domain)
    console.print(f"[green]Rebuilt browse-index.[/] {result.get('message', '')}".rstrip())
    return 0


# -- shared create path ---------------------------------------------------


def _create_scenario(
    spec: dict[str, Any],
    *,
    gen_file: Optional[str],
    inputs: Optional[list[str]],
    overwrite: bool,
    domain: Optional[str],
    allow_cooperation_reset: bool = False,
) -> str:
    """Generate + (optionally) stage sources + render. Returns the new slug."""
    with console.status(f"[cyan]Generating scenario [bold]{spec['name']}[/]…", spinner="dots"):
        entry = control.generate_scenario(
            spec, overwrite=overwrite, domain=domain,
            allow_cooperation_reset=allow_cooperation_reset,
        )
    slug = entry.get("slug")
    console.print(f"[green]Created[/] [bold]{slug}[/].")

    if gen_file or inputs:
        with console.status("[cyan]Staging sources + recompiling…", spinner="dots"):
            control.stage_sources(slug, gen_file=gen_file, inputs=inputs, domain=domain)
        bits = []
        if gen_file:
            bits.append("scenario_gen.py")
        if inputs:
            bits.append(f"{len(inputs)} input(s)")
        console.print(f"[green]Staged[/] {', '.join(bits)} and recompiled.")

    render_scenario_detail(control.get_scenario(slug, domain))
    return slug


# -- interactive (TUI) helpers -------------------------------------------


def _known_solvers(domain: Optional[str] = None) -> list[str]:
    try:
        solvers = control.known_solvers().get("solvers")
        if solvers:
            return solvers
    except control.ControlError:
        pass
    return list(config.SOLVERS)


def _known_policies(domain: Optional[str] = None) -> dict[str, list[str]]:
    """Per-role generation policies from the engine registry (fallback: config)."""
    try:
        policies = control.known_policies().get("policies")
        if policies:
            return policies
    except (control.ControlError, AttributeError):
        pass
    return dict(config.POLICIES)


def _prompt_int(message: str, default: Any, *, min_: int = 1, max_: Optional[int] = None):
    """Validated integer prompt. Returns int, or None if the user aborts (ESC/Ctrl-C)."""
    import questionary

    def _val(text: str):
        try:
            v = int(text)
        except (ValueError, TypeError):
            return "Enter a whole number"
        if v < min_:
            return f"Must be ≥ {min_}"
        if max_ is not None and v > max_:
            return f"Must be ≤ {max_}"
        return True

    ans = questionary.text(message, default=str(default) if default is not None else "", validate=_val).ask()
    return None if ans is None else int(ans)


def _prompt_float(message: str, default: Any):
    import questionary

    def _val(text: str):
        try:
            float(text)
            return True
        except (ValueError, TypeError):
            return "Enter a number"

    ans = questionary.text(message, default=str(default) if default is not None else "", validate=_val).ask()
    return None if ans is None else float(ans)


def _prompt_hauliers(defaults: Optional[list[dict[str, Any]]], *, is_edit: bool):
    """Returns a haulier list, [] for 'use the single default', or None for 'leave unchanged'."""
    import questionary

    if is_edit and defaults:
        tbl = Table(title="Current hauliers", header_style="bold magenta")
        for col in ("Name", "Fleet %", "Order %"):
            tbl.add_column(col)
        for h in defaults:
            tbl.add_row(str(h.get("name", "")), f"{h.get('fleet_share', '')}", f"{h.get('order_share', '')}")
        console.print(tbl)
        if questionary.confirm("Keep existing hauliers?", default=True).ask():
            return None

    if not questionary.confirm("Define hauliers? (No = one default company at 100%)", default=True).ask():
        return []

    hauliers: list[dict[str, Any]] = []
    while True:
        name = questionary.text(f"Haulier #{len(hauliers) + 1} name (blank to finish):").ask()
        if name is None:
            break
        name = name.strip()
        if not name:
            break
        fleet = _prompt_float("  fleet share %:", 0)
        if fleet is None:
            break
        order = _prompt_float("  order share %:", fleet)
        if order is None:
            break
        hauliers.append({"name": name, "fleet_share": fleet, "order_share": order})
        tf = sum(h["fleet_share"] for h in hauliers)
        to = sum(h["order_share"] for h in hauliers)
        console.print(f"  [dim]running totals → fleet {tf:g}% · order {to:g}%[/]")
        if not questionary.confirm("Add another haulier?", default=(tf < 100)).ask():
            break

    if hauliers:
        tf = sum(h["fleet_share"] for h in hauliers)
        to = sum(h["order_share"] for h in hauliers)
        if (abs(tf - 100) > 0.001 or abs(to - 100) > 0.001):
            console.print(
                f"[yellow]Note: shares total fleet {tf:g}% / order {to:g}% — they must each be 100; "
                "generation will reject otherwise.[/]"
            )
    return hauliers


def _spec_defaults(src: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Pull prompt defaults out of a recipe dict (spec.json shape, or a flag-seeded spec)."""
    src = src or {}
    agents = src.get("agents") if isinstance(src.get("agents"), dict) else {}

    def _count(role):
        r = agents.get(role)
        return r.get("count") if isinstance(r, dict) else None

    def _policy(role):
        r = agents.get(role)
        pol = r.get("policy") if isinstance(r, dict) else None
        return pol.get("type") if isinstance(pol, dict) else None

    return {
        "name": src.get("name"),
        "trucks": _count("truck") or 30,
        "orders": _count("order") or 200,
        "facilities": _count("facility") or 20,
        "days": src.get("simulationDays") or 1,
        "unit": src.get("orderCountUnit") or "per_day",
        "solver": src.get("solver"),
        "hauliers": src.get("hauliers"),
        "seed": src.get("seed"),
        "truck_policy": _policy("truck"),
        "order_policy": _policy("order"),
        "facility_policy": _policy("facility"),
    }


def _preview_spec(spec: dict[str, Any], gen_file, inputs) -> None:
    agents = spec.get("agents") or {}
    lines = [
        f"[bold]{spec.get('name')}[/]",
        f"days={spec.get('simulationDays')}  trucks={agents.get('truck', {}).get('count')}  "
        f"orders={agents.get('order', {}).get('count')} ({spec.get('orderCountUnit', 'per_day')})  "
        f"facilities={agents.get('facility', {}).get('count')}",
        f"solver={spec.get('solver') or 'engine default'}  hauliers={len(spec.get('hauliers') or []) or 'default'}",
    ]

    def _pol(role):
        r = agents.get(role) or {}
        p = r.get("policy") if isinstance(r, dict) else None
        return (p or {}).get("type") if isinstance(p, dict) else None

    pols = [f"{r}={_pol(r)}" for r in ("truck", "order", "facility") if _pol(r)]
    if pols:
        lines.append("policies: " + "  ".join(pols) + (f"   seed={spec['seed']}" if spec.get("seed") is not None else ""))
    if gen_file:
        lines.append(f"scenario_gen.py: {gen_file}")
    if inputs:
        lines.append(f"inputs: {', '.join(inputs)}")
    console.print(Panel("\n".join(lines), border_style="cyan", title="new scenario"))


def interactive_build_spec(domain: Optional[str], *, defaults: Optional[dict[str, Any]] = None, is_edit: bool):
    """Questionary wizard → ``{"spec", "gen_file", "inputs"}`` or None if aborted."""
    import questionary

    d = _spec_defaults(defaults)
    name = questionary.text("Scenario name:", default=d["name"] or "").ask()
    if name is None or not name.strip():
        return None
    trucks = _prompt_int("Trucks:", d["trucks"])
    if trucks is None:
        return None
    orders = _prompt_int("Orders:", d["orders"])
    if orders is None:
        return None
    unit = questionary.select(
        "Order count is…", choices=["per_day", "total"], default=d["unit"]
    ).ask()
    if unit is None:
        return None
    facilities = _prompt_int("Facilities:", d["facilities"])
    if facilities is None:
        return None
    days = _prompt_int("Simulation days:", d["days"], min_=1, max_=7)
    if days is None:
        return None
    solvers = _known_solvers(domain)
    solver = questionary.select(
        "Default solver:", choices=solvers, default=d["solver"] if d["solver"] in solvers else solvers[0]
    ).ask()
    if solver is None:
        return None

    # Per-role generation policy (the new policy-based datagen). Blank/first = default.
    policies = _known_policies(domain)

    def _pick_policy(role: str, prompt: str) -> Optional[str]:
        choices = policies.get(role) or config.POLICIES.get(role, [])
        if not choices:
            return None
        default = d.get(f"{role}_policy") or config.DEFAULT_POLICIES.get(role, choices[0])
        return questionary.select(
            prompt, choices=choices, default=default if default in choices else choices[0]
        ).ask()

    truck_policy = _pick_policy("truck", "Truck policy (how trucks are generated):")
    if truck_policy is None:
        return None
    order_policy = _pick_policy("order", "Order policy (how orders/OD are generated):")
    if order_policy is None:
        return None
    facility_policy = _pick_policy("facility", "Facility policy (how facility sites are chosen):")
    if facility_policy is None:
        return None

    order_agent: dict[str, Any] = {"count": orders, "policy": {"type": order_policy}}
    # Historical order policy needs a data file (read at compile to fit the matrix + curve).
    if order_policy == "historical":
        src = questionary.path("Historical order data file (JSON/CSV/Excel):").ask()
        if src and src.strip():
            order_agent["policy"]["source"] = os.path.abspath(src.strip())

    seed_raw = questionary.text(
        "Random seed (blank = engine default; fixed seed = reproducible):",
        default=str(d.get("seed")) if d.get("seed") is not None else "",
    ).ask()

    spec: dict[str, Any] = {
        "name": name.strip(),
        "simulationDays": days,
        "orderCountUnit": unit,
        "agents": {
            "truck": {"count": trucks, "policy": {"type": truck_policy}},
            "order": order_agent,
            "facility": {"count": facilities, "policy": {"type": facility_policy}},
        },
        "solver": solver,
    }
    if seed_raw and seed_raw.strip():
        try:
            spec["seed"] = int(seed_raw.strip())
        except ValueError:
            pass
    hauliers = _prompt_hauliers(d["hauliers"], is_edit=is_edit)
    if hauliers:
        spec["hauliers"] = hauliers

    # Optional Tier-2 sources + matrix/curve files.
    gen_file = None
    inputs: list[str] = []
    if questionary.confirm("Attach a scenario_gen.py datagen override?", default=False).ask():
        path = questionary.path("Path to scenario_gen.py:").ask()
        if path and path.strip():
            gen_file = path.strip()
    while questionary.confirm("Stage a raw input file (inputs/)?", default=False).ask():
        path = questionary.path("Path to input file:").ask()
        if path and path.strip():
            inputs.append(path.strip())
        else:
            break
    _maybe_prompt_for_input_files(spec)

    _preview_spec(spec, gen_file, inputs)
    if not questionary.confirm("Create this scenario?" if not is_edit else "Apply these settings?", default=True).ask():
        return None
    return {"spec": spec, "gen_file": gen_file, "inputs": inputs}


def pick_slug(domain: Optional[str], message: str) -> Optional[str]:
    import questionary

    scenarios = control.list_scenarios(domain)
    if not scenarios:
        console.print("[yellow]No scenarios on disk yet.[/]")
        return None
    choices = [
        questionary.Choice(
            f"{s.get('name', s.get('slug'))}  [{s.get('slug')}]", value=s.get("slug")
        )
        for s in scenarios
    ]
    return questionary.select(message, choices=choices).ask()


def interactive_new(domain: Optional[str] = None, *, seed: Optional[dict[str, Any]] = None) -> int:
    built = interactive_build_spec(domain, defaults=seed, is_edit=False)
    if built is None:
        console.print("[dim]Cancelled.[/]")
        return 1
    spec = built["spec"]
    spec.setdefault("orderCountUnit", "per_day")
    try:
        _create_scenario(spec, gen_file=built["gen_file"], inputs=built["inputs"], overwrite=False, domain=domain)
    except control.ControlError as exc:
        import questionary

        if "already exists" in str(exc).lower() and questionary.confirm(
            "A scenario with that slug exists. Overwrite it?", default=False
        ).ask():
            _create_scenario(spec, gen_file=built["gen_file"], inputs=built["inputs"], overwrite=True, domain=domain)
        else:
            raise
    return 0


def interactive_edit(domain: Optional[str] = None, slug: Optional[str] = None) -> int:
    import questionary

    if not slug:
        slug = pick_slug(domain, "Edit which scenario?")
    if not slug:
        return 1
    mode = questionary.select(
        f"Edit {slug} —", choices=["Guided field edits", "Open spec.json in $EDITOR", "Cancel"]
    ).ask()
    if mode in (None, "Cancel"):
        console.print("[dim]Cancelled.[/]")
        return 0

    current = control.get_spec(slug, domain)
    if mode == "Open spec.json in $EDITOR":
        edited = _editor_patch(current)
        if edited is None:
            console.print("[dim]No changes.[/]")
            return 0
        patch: dict[str, Any] = edited
    else:
        built = interactive_build_spec(domain, defaults=current, is_edit=True)
        if built is None:
            console.print("[dim]Cancelled.[/]")
            return 0
        patch = built["spec"]

    with console.status(f"[cyan]Recompiling [bold]{slug}[/]…", spinner="dots"):
        control.edit_scenario(slug, patch, domain=domain)
    console.print(f"[green]Edited[/] [bold]{slug}[/].")

    # In guided mode the wizard can also (re)stage sources.
    if mode == "Guided field edits" and (built["gen_file"] or built["inputs"]):
        with console.status("[cyan]Staging sources + recompiling…", spinner="dots"):
            control.stage_sources(slug, gen_file=built["gen_file"], inputs=built["inputs"], domain=domain)
        console.print("[green]Staged sources and recompiled.[/]")

    render_scenario_detail(control.get_scenario(slug, domain))
    return 0


def interactive_show(domain: Optional[str] = None) -> int:
    slug = pick_slug(domain, "Show which scenario?")
    if not slug:
        return 1
    render_scenario_detail(control.get_scenario(slug, domain))
    return 0


def interactive_delete(domain: Optional[str] = None) -> int:
    import questionary

    slug = pick_slug(domain, "Delete which scenario?")
    if not slug:
        return 1
    if not questionary.confirm(f"Delete scenario {slug}? This cannot be undone.", default=False).ask():
        console.print("[dim]Aborted.[/]")
        return 0
    control.delete_scenario(slug, domain)
    console.print(f"[green]Deleted[/] [bold]{slug}[/].")
    return 0


# -- argparse registration -----------------------------------------------


def _has_authoring_flags(args) -> bool:
    return any(
        getattr(args, name, None) not in (None, [], False)
        for name in ("name", "trucks", "orders", "facilities", "days", "solver",
                     "haulier", "early_orders", "order_unit", "gen_file", "input",
                     "truck_policy", "order_policy", "facility_policy", "order_source", "seed")
    )


def _add_authoring_flags(p, *, require_name: bool) -> None:
    p.add_argument("--name", help="Human-readable scenario name" + ("" if require_name else " (optional)"))
    p.add_argument("--slug", help="Explicit slug (default: slugified name)")
    p.add_argument("--days", type=int, help="Simulation horizon in days")
    p.add_argument("--trucks", type=int, help="Truck count")
    p.add_argument("--orders", type=int, help="Order count (per --order-unit)")
    p.add_argument("--facilities", type=int, help="Facility count")
    p.add_argument("--order-unit", choices=["per_day", "total"], default=None,
                   help="Whether --orders is a per-day rate or a run total (default: per_day on new)")
    p.add_argument("--early-orders", type=int, help="Number of orders released at t=0")
    p.add_argument("--solver", choices=config.SOLVERS,
                   help="Default assignment solver (else a bad name is silently coerced to the engine default)")
    p.add_argument("--haulier", action="append",
                   help="Haulier 'Name:fleet%%:order%%' (repeatable; shares must total 100)")
    p.add_argument("--demand-curve-file",
                   help="24h order-demand curve as JSON, CSV, or Excel (.xlsx) — see templates")
    p.add_argument("--trip-matrix-file",
                   help="pickup/dropoff probability matrix as JSON, CSV, or Excel (.xlsx) — see templates")
    # Per-role generation policy selection (the new policy-based datagen).
    p.add_argument("--truck-policy", choices=config.POLICIES["truck"],
                   help="Truck generation policy (default: %s)" % config.DEFAULT_POLICIES["truck"])
    p.add_argument("--order-policy", choices=config.POLICIES["order"],
                   help="Order generation policy (default: %s)" % config.DEFAULT_POLICIES["order"])
    p.add_argument("--facility-policy", choices=config.POLICIES["facility"],
                   help="Facility generation policy (default: %s)" % config.DEFAULT_POLICIES["facility"])
    p.add_argument("--order-source",
                   help="Historical data file (JSON/CSV/Excel) for --order-policy historical")
    p.add_argument("--seed", type=int,
                   help="Master seed for reproducible generation (default: engine default)")
    p.add_argument("--gen-file", help="Path to a scenario_gen.py Tier-2 datagen override")
    p.add_argument("--input", action="append", help="Raw input file to stage into inputs/ (repeatable)")


def _add_domain(p) -> None:
    p.add_argument("--domain", default=None, help="Scenario domain dir override")


def register(sub) -> None:
    p = sub.add_parser("scenario", help="Construct and maintain scenarios")
    ssub = p.add_subparsers(dest="scenario_cmd", required=True)

    lst = ssub.add_parser("list", help="List scenarios on disk")
    lst.add_argument("--json", action="store_true", help="Emit raw JSON")
    _add_domain(lst)
    lst.set_defaults(func=_cmd_list)

    show = ssub.add_parser("show", help="Show a scenario's detail")
    show.add_argument("slug")
    show.add_argument("--json", action="store_true", help="Emit raw JSON")
    _add_domain(show)
    show.set_defaults(func=_cmd_show)

    new = ssub.add_parser("new", help="Generate a new scenario (flags and/or --spec-file)")
    new.add_argument("--spec-file", help="JSON recipe to use as the base (flags override its fields)")
    new.add_argument("--overwrite", action="store_true", help="Replace an existing scenario of the same slug")
    new.add_argument(
        "--allow-cooperation-reset",
        action="store_true",
        help="Permit a save that EMPTIES a structure's cooperation edges/pools "
             "(refused by default so a client that does not speak the field "
             "cannot silently delete it)",
    )
    _add_authoring_flags(new, require_name=True)
    _add_domain(new)
    new.set_defaults(func=_cmd_new)

    edit = ssub.add_parser("edit", help="Edit a scenario's recipe and recompile (keeps its sources)")
    edit.add_argument("slug")
    edit.add_argument("--spec-file", help="JSON patch to merge over the current recipe")
    edit.add_argument("--set", action="append", help="Patch one field: key=value (repeatable)")
    edit.add_argument(
        "--allow-cooperation-reset",
        action="store_true",
        help="Permit a patch that EMPTIES a structure's cooperation edges/pools",
    )
    _add_authoring_flags(edit, require_name=False)
    _add_domain(edit)
    edit.set_defaults(func=_cmd_edit)

    comp = ssub.add_parser("compile", help="Regenerate scenario.json from the folder's sources (spec.json)")
    comp.add_argument("slug")
    comp.add_argument("--reseed", action="store_true",
                      help="Draw a fresh master seed so regeneration produces NEW data")
    comp.add_argument("--baseline-rules", action="store_true",
                      help="Re-record facilityRulesWorld against the world this spec now "
                           "generates BEFORE compiling (see `scenario rules-baseline`). "
                           "Records the new world; does NOT check the rules still mean "
                           "what you intended")
    comp.add_argument("-y", "--yes", action="store_true",
                      help="Skip the --baseline-rules confirmation prompt")
    _add_domain(comp)
    comp.set_defaults(func=_cmd_compile)

    rbase = ssub.add_parser(
        "rules-baseline",
        help="Re-record facilityRulesWorld for a scenario whose facility world moved "
             "(shows what each facilityRule now matches; records the new world — does "
             "NOT validate that the rules still mean what you intended)",
    )
    rbase.add_argument("slug")
    rbase.add_argument("-y", "--yes", action="store_true", help="Skip the confirmation prompt")
    _add_domain(rbase)
    rbase.set_defaults(func=_cmd_rules_baseline)

    dele = ssub.add_parser("delete", help="Delete a scenario folder")
    dele.add_argument("slug")
    dele.add_argument("-y", "--yes", action="store_true", help="Skip the confirmation prompt")
    _add_domain(dele)
    dele.set_defaults(func=_cmd_delete)

    ridx = ssub.add_parser("reindex", help="Rebuild the dashboard browse-index from scenario.json files")
    _add_domain(ridx)
    ridx.set_defaults(func=_cmd_reindex)
