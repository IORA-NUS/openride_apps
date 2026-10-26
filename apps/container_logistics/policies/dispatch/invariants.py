"""The planner contract, checked: ``normalise_result`` then ``check_result(result, tick) -> list[str]``.

The runner calls these on every tick's result before anything is committed, and
``Tick.call`` (through ``_TickShared.verify``) on every sub-tick's; a non-empty answer
discards that result (message 18 for the whole tick, 19 for a sub-tick). Replaces
``check_plan`` (plan ``plan_contract_final.md`` §2.6). :func:`check_wire_notes` is the
runner's extra check of the WHOLE tick's ``solver_notes``, the only notes that reach the
wire.

Framework side of the boundary. Pure: reads the result and the documents; draws no RNG;
imports nothing from ``assignment``.

What makes the check hold against a DISHONEST solver (review H2, N1-N3):

- **The result is read ONCE.** :func:`normalise_result` runs immediately after
  ``solve_tick`` returns and copies the result into plain data with exact builtin types:
  pair ids must already be exact ``str`` (``type(x) is str``; a ``str`` subclass could
  hash as one id while checked and another at commit), note keys become plain tuples of
  plain strings, note values plain JSON (``dict``/``list``/``str``/``int``/``float``/
  ``bool``/``None``). Anything malformed is replaced by a sentinel that carries its
  ``repr``, taken at that moment. From then on no solver-supplied object is iterated,
  hashed, compared or printed: a lying mapping, a chameleon id or a note whose
  ``__iter__`` rewrites a document has already run, before any check, and the wire is
  built from the copy.
- **The check returns the documents it judged** (``check_result(..., judged=[])``). The
  commit and the verified-pair memo use exactly those ``(truck_doc, order_doc)`` pairs;
  nothing looks a pair up by id again after the check.
- **The check uses the framework's own state**: the callers pass ``permitted`` (the
  tick's own predicate: at the top ownership-or-declared AND physical, with the
  verified-pair memo of sub-ticks the framework itself checked), ``index`` (the tick's
  own documents by id) and, from ``Tick.call``, ``intact`` (whether a document is
  unmodified since the tick was built) -- never anything read back from an attribute a
  solver could have rewritten. The runner's document fingerprint scan over every truck
  and every committed order is the LAST step before the commit.

So at the top a cross-company pair the top did not declare, or a physically infeasible
pair, is a violation whatever solver produced it, however it tampered with its values.
Called with the defaults (tests, the battery), ``check_result`` normalises the result
itself and reads ``tick.permitted`` / ``tick.id_index``, which are read-only properties
over that same state.

Threat model (``docs/policy_authoring.md`` "What the framework enforces"): in scope are a
solver's honest bugs, crafted VALUES flowing through the contract (subclassed ``str`` /
``dict``, non-JSON, mutation of documents, world or tick state) and anything that would
silently corrupt the research metric. Out of scope, documented as a limit:
interpreter-level tampering -- reflection (closure ``cell_contents``,
``object.__setattr__``, ``__class__`` swaps, ``gc``) and monkeypatching framework
classes or modules (this module included). A monkeypatch also PERSISTS: agents run in
long-lived celery workers, so a patched framework function stays patched for every
later run in that worker until ``openride-celery`` is restarted.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any, Callable, Iterable, Mapping

from .result import PREFETCH_MAX, Result

if TYPE_CHECKING:
    from .tick import Tick

logger = logging.getLogger(__name__)

__all__ = ["check_result", "check_wire_notes", "normalise_result"]

_INF = float("inf")


class _Shown:
    """A malformed value of a result, replaced by its ``repr`` taken during normalisation.

    Never equal to anything but itself and hashed by identity, so a check can test it,
    count it and print it without running solver code.
    """

    __slots__ = ("text",)

    def __init__(self, text: str) -> None:
        self.text = text

    def __repr__(self) -> str:
        return self.text


class _NotJSON:
    """A note value that is not plain JSON: the reason, reported by :func:`check_wire_notes`."""

    __slots__ = ("reason",)

    def __init__(self, reason: str) -> None:
        self.reason = reason

    def __repr__(self) -> str:
        return f"<not JSON: {self.reason}>"


def _shown(value: Any) -> _Shown:
    try:
        return _Shown(repr(value))
    except Exception as exc:  # noqa: BLE001 - a repr that raises is still shown
        return _Shown(f"<{type(value).__name__}: repr raised {type(exc).__name__}>")


def _json_key(key: Any) -> str:
    """A nested mapping key as ``json.dumps`` writes it (str, float, bool, None, int), else raise like it."""
    if type(key) is str:
        return key
    if isinstance(key, str):
        return str.__str__(key)
    if isinstance(key, float):
        value = float.__float__(key)
        if value != value or value in (_INF, -_INF):
            raise ValueError("Out of range float values are not JSON compliant")
        return float.__repr__(value)
    if key is True:
        return "true"
    if key is False:
        return "false"
    if key is None:
        return "null"
    if isinstance(key, int):
        return int.__repr__(int.__int__(key))
    raise TypeError(f"keys must be str, int, float, bool or None, not {type(key).__name__}")


def _plain(value: Any, active: set[int]) -> Any:
    """``value`` as plain JSON with exact builtin types, or raise like ``json.dumps(allow_nan=False)``.

    Accepts what ``json.dumps`` accepts (subclasses of ``str``/``int``/``float``/``list``/
    ``tuple``/``dict`` included, read once) and returns exact types: a ``tuple`` becomes
    a ``list`` and a nested key a ``str``, exactly as the wire's JSON would spell them.
    """
    kind = type(value)
    if value is None or kind is str or kind is bool or kind is int:
        return value
    if kind is float or isinstance(value, float):
        out = value if kind is float else float.__float__(value)
        if out != out or out in (_INF, -_INF):
            raise ValueError("Out of range float values are not JSON compliant")
        return out
    if isinstance(value, str):
        return str.__str__(value)
    if isinstance(value, int):
        return int.__int__(value)
    if isinstance(value, (list, tuple, dict)):
        marker = id(value)
        if marker in active:
            raise ValueError("Circular reference detected")
        active.add(marker)
        try:
            if isinstance(value, dict):
                return {_json_key(k): _plain(v, active) for k, v in list(value.items())}
            return [_plain(v, active) for v in list(value)]
        finally:
            active.discard(marker)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _plain_or_reason(value: Any) -> tuple[Any, str | None]:
    try:
        return _plain(value, set()), None
    except (TypeError, ValueError, RecursionError) as exc:
        return None, str(exc) or type(exc).__name__


def _plain_pair(pair: Any) -> Any:
    """``pair`` itself when it is an exact ``tuple`` of two exact ``str`` (N1), else its :class:`_Shown`."""
    if type(pair) is tuple and len(pair) == 2 and type(pair[0]) is str and type(pair[1]) is str:
        return pair
    if type(pair) is tuple and len(pair) == 2:
        # Name a str subclass: its repr reads exactly like the plain id it is not.
        try:
            return _Shown("(" + ", ".join(repr(x) if type(x) is str or not isinstance(x, str)
                                          else f"<{type(x).__name__} {str.__repr__(x)}>" for x in pair) + ")")
        except Exception:  # noqa: BLE001 - fall back to the plain repr
            pass
    return _shown(pair)


def _plain_note_key(key: Any) -> Any:
    """A note key as a plain ``(str, str)`` tuple (a tuple subclass of two exact strings is read once), else shown."""
    if isinstance(key, tuple):
        try:
            if tuple.__len__(key) == 2:
                a, b = tuple.__getitem__(key, 0), tuple.__getitem__(key, 1)
                if type(a) is str and type(b) is str:
                    return (a, b)
        except Exception:  # noqa: BLE001 - shown below
            pass
    return _shown(key)


def _plain_mapping(value: Any, *, strict: bool) -> tuple[Any, list[tuple[str, str]]]:
    """A mapping with string keys as a plain ``dict`` (keys exact ``str``), plus each value's JSON failure.

    Returns ``(_Shown, [])`` for a value that is not a mapping, or one with a non-string
    key. A value that is not plain JSON is kept as given unless ``strict``, in which case it
    is replaced by a :class:`_NotJSON` and listed as ``(key, reason)``.
    """
    if not isinstance(value, Mapping):
        return _shown(value), []
    try:
        items = list(value.items())
    except Exception:  # noqa: BLE001 - an unreadable mapping is malformed
        return _shown(value), []
    if not all(isinstance(k, str) for k, _v in items):
        return _shown(value), []
    out: dict[str, Any] = {}
    failures: list[tuple[str, str]] = []
    for k, v in items:
        key = k if type(k) is str else str.__str__(k)
        plain, reason = _plain_or_reason(v)
        if reason is None:
            out[key] = plain
        elif strict:
            out[key] = _NotJSON(reason)
            failures.append((key, reason))
        else:
            out[key] = v
    return out, failures


def normalise_result(result: Any, *, strict: bool = False) -> tuple[Any, list[str]]:
    """``result`` copied into plain data with exact builtin types, read ONCE (review N1-N3); plus problems.

    Call immediately after ``solve_tick`` returns, before any check. A value that is not a
    :class:`Result` is returned as is (``check_result`` reports it). Otherwise returns a
    NEW ``Result`` whose
    - ``pairs`` are exact ``(str, str)`` tuples, a malformed pair replaced by its ``repr``;
    - ``solver_notes`` keys are plain ``(str, str)`` tuples and values plain ``dict`` objects
      with ``str`` keys (a malformed key or note replaced by its ``repr``);
    - ``tick_notes`` a plain ``dict`` with ``str`` keys;
    - ``prefetch`` a tuple of exact ``str`` order ids, first occurrence kept
      (:func:`_plain_prefetch`; a malformed entry or more than ``PREFETCH_MAX`` ids is an
      R7 problem when ``strict``).

    ``strict`` (the runner, for the whole tick): every note value must be plain JSON; a
    solver note value that is not is replaced by a marker :func:`check_wire_notes` reports,
    and ``tick_notes`` that are not a JSON mapping with string keys are returned as
    problems. Not ``strict`` (``Tick.call``, the battery): values that are not JSON are kept
    as given -- a sub-tick's notes reach the wire only through the top's own result, which
    is normalised strictly. Never raises because of the result.
    """
    if not isinstance(result, Result):
        return result, []
    problems: list[str] = []
    try:
        raw_pairs = list(result.pairs)
    except Exception:  # noqa: BLE001 - an unreadable pair list is one malformed pair
        raw_pairs = [_shown(getattr(result, "pairs", None))]
    pairs = tuple(_plain_pair(p) for p in raw_pairs)
    notes: dict[Any, Any] = {}
    try:
        note_items = list(result.solver_notes.items())
    except Exception:  # noqa: BLE001
        note_items = [(_shown(getattr(result, "solver_notes", None)), {})]
    for key, note in note_items:
        plain_key = _plain_note_key(key)
        plain_note, _failures = _plain_mapping(note, strict=strict)
        notes[plain_key] = plain_note
    tick_notes, failures = _plain_mapping(getattr(result, "tick_notes", None) or {}, strict=strict)
    if type(tick_notes) is _Shown:
        if strict:
            problems.append(f"tick_notes {tick_notes!r} is not a mapping with string keys")
        tick_notes = {}
    elif failures:
        problems.extend(f"tick_notes[{k!r}] is not plain JSON ({reason})" for k, reason in failures)
        tick_notes = {}
    prefetch, prefetch_problems = _plain_prefetch(getattr(result, "prefetch", ()))
    if strict:
        problems.extend(prefetch_problems)
    return Result(pairs=pairs, solver_notes=notes, tick_notes=tick_notes, prefetch=prefetch), problems


def _plain_prefetch(value: Any) -> tuple[tuple[str, ...], list[str]]:
    """``value`` as a tuple of exact ``str`` order ids (first occurrence kept), plus R7 problems.

    Only exact ``str`` entries are kept (a ``str`` subclass could read as one id now and
    another at the fetch); a malformed entry, a value that is not a tuple, or more than
    ``PREFETCH_MAX`` distinct ids is a problem (message 18 at the top; a sub-tick's
    prefetch is only copied, its problems are not reported). The kept ids are returned
    either way, so a non-strict caller still sees the clean subset.
    """
    if value is None:
        return (), []
    if type(value) is not tuple:
        return (), [f"prefetch {_shown(value)!r} is not a sequence of order id strings"]
    if not value:
        return (), []
    problems: list[str] = []
    kept: dict[str, None] = {}
    for entry in value:
        if type(entry) is str and entry:
            kept.setdefault(entry, None)
        else:
            problems.append(f"prefetch entry {_shown(entry)!r} is not an order id string")
            if len(problems) >= 8:
                break
    if len(kept) > PREFETCH_MAX:
        problems.append(f"prefetch names {len(kept)} ids, more than the cap {PREFETCH_MAX}")
    return tuple(kept), problems


def check_result(result: Any, tick: "Tick", *, permitted: Any = None, index: Any = None,
                 intact: Callable[[Any], bool] | None = None, judged: list | None = None,
                 normalised: bool = False) -> list[str]:
    """Every way ``result`` breaks the contract for ``tick``; ``[]`` when it is valid.

    Reports: a value that is not a :class:`Result`; a pair that is not two exact strings;
    a truck or order id not in scope; a truck or an order appearing twice; a paired
    document that ``intact`` says was modified during the solve; a pair that
    ``permitted`` rejects (default ``tick.permitted``; the runner passes the top tick's
    verified-pair-aware check); a ``solver_notes`` key that is not one of the result's
    pairs, or a value that is not a mapping with string keys. ``index`` defaults to
    ``tick.id_index`` (first document per id).

    ``result`` is normalised first (:func:`normalise_result`) unless the caller already
    did (``normalised=True``: the runner and ``Tick.call`` pass the copy they normalised).
    ``judged``, when given, receives the ``(truck_doc, order_doc)`` pairs this check judged,
    in result order (only meaningful when the answer is ``[]``): the commit and the
    verified-pair memo use exactly these, never a second lookup by id (review N1). Pure.
    """
    if not isinstance(result, Result):
        return [f"solve_tick() returned {type(result).__name__}, expected Result"]
    if not normalised:
        result, _ = normalise_result(result)
    allowed = tick.permitted if permitted is None else permitted
    trucks, orders = tick.id_index if index is None else index
    scope = tick.scope
    problems: list[str] = []
    seen_trucks: set[str] = set()
    seen_orders: set[str] = set()
    valid_pairs: set[tuple[str, str]] = set()
    docs: list[tuple[Any, Any]] = []
    for pair in result.pairs:
        if not (type(pair) is tuple and len(pair) == 2 and type(pair[0]) is str and type(pair[1]) is str):
            problems.append(f"pair {pair!r} is not a (truck_id, order_id) pair of strings")
            continue
        tid, oid = pair
        valid_pairs.add(pair)
        if tid in seen_trucks:
            problems.append(f"truck {tid!r} appears twice in the result")
        seen_trucks.add(tid)
        if oid in seen_orders:
            problems.append(f"order {oid!r} appears twice in the result")
        seen_orders.add(oid)
        t = trucks.get(tid)
        o = orders.get(oid)
        if t is None:
            problems.append(f"truck {tid!r} is not in scope {scope}")
        if o is None:
            problems.append(f"order {oid!r} is not in scope {scope}")
        if t is not None and o is not None:
            if intact is not None and not (intact(t) and intact(o)):
                which = " and ".join(kind + " " + repr(i) for kind, i, doc in (("truck", tid, t), ("order", oid, o))
                                     if not intact(doc))
                problems.append(f"{which} was modified during the solve (documents are read-only)")
            elif not allowed(t, o):
                problems.append(f"pair ({tid!r}, {oid!r}) is not allowed")
            docs.append((t, o))
    for key, note in result.solver_notes.items():
        if type(key) is not tuple or key not in valid_pairs:
            problems.append(f"solver_notes key {key!r} is not one of the result's pairs")
        elif type(note) is not dict:
            problems.append(f"solver_notes for {key!r} is not a mapping with string keys")
    if judged is not None and not problems:
        judged.extend(docs)
    return problems


def check_wire_notes(result: Result, reserved: Iterable[str]) -> list[str]:
    """Problems with the WHOLE tick's ``solver_notes`` as they will reach the wire (review L3).

    A note may not use a ``reserved`` key (a field the framework writes on the wire
    itself, ``compat.wire.reserved_note_keys_for``), and every note value must be plain
    JSON (no object, no NaN or infinity): the wire would otherwise carry a
    ``default=str`` text that differs from run to run. Call after :func:`check_result`
    (it assumes every note is a mapping with string keys), on the runner's strictly
    normalised result (:func:`normalise_result`), whose values are plain data or a
    marker for the value that was not JSON. Pure.
    """
    reserved = frozenset(reserved)
    problems: list[str] = []
    for key, note in result.solver_notes.items():
        if not isinstance(note, Mapping):
            continue
        forged = sorted(k for k in note if k in reserved)
        if forged:
            problems.append(f"solver_notes for {key!r} sets the framework wire field(s) "
                            f"{', '.join(map(repr, forged))}; a solver's notes may not")
        bad = next((v for v in note.values() if type(v) is _NotJSON), None)
        if bad is not None:
            problems.append(f"solver_notes for {key!r} is not plain JSON ({bad.reason})")
            continue
        try:
            json.dumps(dict(note), allow_nan=False)
        except (TypeError, ValueError) as exc:
            problems.append(f"solver_notes for {key!r} is not plain JSON ({exc})")
    return problems
