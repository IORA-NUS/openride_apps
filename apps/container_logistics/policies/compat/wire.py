"""Wire-format details that name old solver provenance: the share tags and the payload fields.

Single responsibility: render a framework :class:`~apps.container_logistics.assignment.
accounting.ShareAccount` plus a solver's opaque per-pair ``solver_notes`` into one of
the two historical share-tag formats, and turn a tag into the keyword set of an
``AssignedHaulTripPayload``. Everything here exists for byte identity with runs made
before the generic contract; nothing here is read by a solver.

- :data:`LEGACY_NOTE_FIELDS` -- the note keys that are routed to their OLD top-level
  wire fields (``pool_id``, ``market_round``: the market's provenance). The table is
  keyed on note-key NAMES, not on which solver produced them.
- :func:`split_notes` -> ``(legacy, rest)``; ``rest`` rides in a ``solver_notes`` tag
  key / payload field that is OMITTED when empty, so no shipped run changes a byte (no
  built-in leaf emits a non-legacy note).
- :func:`render_v1_tag` (moved verbatim from ``accounting._v1_tag``) and
  :func:`render_audited_tag` (moved verbatim from ``accounting._audited_tag``).
- :func:`share_tag_renderer` -> the per-pair renderer for a format name; the only place
  the format names are spelled.
- :func:`wire_fields` -> the ``publish()`` keyword set (moved from ``assignment/app.py``).
- :data:`LEGACY_NOTE_SOURCES` / :func:`reserved_note_keys_for` -> which note keys a
  run's top may NOT use (review L3): every framework wire field, except the legacy
  fields for exactly the ``PooledMarket`` top whose market provenance they are (an
  explicit NAME-keyed compat rule, like the share-tag format's).
- :data:`V1_SHARE_TAG_BUILTINS` / :func:`share_tag_format_for` -> WHICH format a run
  uses: ``v1`` for exactly the pre-v2 ``Partitioned`` top (an explicit NAME-keyed rule,
  moved from the registry's retired ``share_tag_format`` registration field; no
  registration can opt in), ``audited`` for everything else; :func:`stamp_fields` ->
  the stamp's ``share_tag_format`` entry.

LIMIT, by design: notes surface only on SHARED (cross-company) pairs. Tags exist only
for the accounts ``accounting.account_shares`` produces, and the truck agent copies
the payload's collaboration fields into the trip only when ``shared`` is set
(``truck/app.py``). A note on a self-serve is dropped.

Leaf module: imports nothing from ``apps.container_logistics.assignment`` at module
top (``assignment.accounting`` imports this module at its top, and ``assignment``'s
``__init__`` eagerly imports ``app``). The one helper it shares with the accounting,
``finite_or_none``, is imported inside :func:`render_audited_tag`.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping

__all__ = [
    "FRAMEWORK_WIRE_FIELDS",
    "LEGACY_NOTE_FIELDS",
    "LEGACY_NOTE_SOURCES",
    "V1_SHARE_TAG_BUILTINS",
    "render_audited_tag",
    "render_v1_tag",
    "reserved_note_keys_for",
    "share_tag_format_for",
    "share_tag_renderer",
    "split_notes",
    "stamp_fields",
    "wire_fields",
]

#: Note keys routed to their old top-level wire fields (the audited tag's 5th and 8th
#: keys). Moved from ``accounting.PAIR_NOTE_KEYS``.
LEGACY_NOTE_FIELDS = ("pool_id", "market_round")

#: The payload keywords ``publish()`` passed before the generic channel, in that order,
#: with ``shared`` defaulting to ``False`` and every other field to ``None``.
_TAG_WIRE_FIELDS = ("owner_haulier_id", "carrier_haulier_id", "benefit_km", "pool_id",
                    "awarded_cost_km", "owner_reserve_km", "market_round")

#: The tag key / payload field carrying every note key that is not a legacy field.
SOLVER_NOTES_FIELD = "solver_notes"

#: THE ``(slot, name)`` tops whose runs render the pre-v2 ``v1`` share tags (byte identity
#: with pre-v2 partitioned runs); every other top renders ``audited``. Moved from the
#: registry (``framework/registry.py``, message 15 retired: a registration can no longer
#: ask for ``v1``). Keyed on the registered NAME, not the class: one class backs one name,
#: and built-in discovery registers ``Partitioned`` first, so another class claiming the
#: name fails loud in the registry. Never add to this set.
V1_SHARE_TAG_BUILTINS = frozenset({("planner", "Partitioned")})

#: Every payload field the framework writes from a share tag (plus the notes field itself).
FRAMEWORK_WIRE_FIELDS = ("shared",) + _TAG_WIRE_FIELDS + (SOLVER_NOTES_FIELD,)

#: THE ``(slot, name)`` tops whose notes may write :data:`LEGACY_NOTE_FIELDS` (the market's
#: round provenance, byte identity with every pooled run). Any other top writing
#: ``pool_id`` / ``market_round`` would forge that provenance on the wire. Never add to it.
LEGACY_NOTE_SOURCES = frozenset({("planner", "PooledMarket")})


def reserved_note_keys_for(name: str, *, registered: bool = True) -> frozenset[str]:
    """The note keys a run whose TOP planner is ``name`` may not use: framework wire fields.

    :data:`FRAMEWORK_WIRE_FIELDS`, minus :data:`LEGACY_NOTE_FIELDS` for a registered
    top in :data:`LEGACY_NOTE_SOURCES`.
    """
    reserved = frozenset(FRAMEWORK_WIRE_FIELDS)
    if registered and ("planner", name) in LEGACY_NOTE_SOURCES:
        reserved -= frozenset(LEGACY_NOTE_FIELDS)
    return reserved


def share_tag_format_for(name: str, *, registered: bool = True) -> str:
    """The share-tag format of a run whose TOP planner is ``name``: ``"v1"`` or ``"audited"``.

    ``registered=False`` (a hand-injected unregistered class, whose name is only its
    class name) is always ``audited``, as it was when the format was a registration field.
    """
    return "v1" if registered and ("planner", name) in V1_SHARE_TAG_BUILTINS else "audited"


def stamp_fields(top_name: str) -> dict[str, Any]:
    """The run stamp's compat entries for top planner ``top_name``: ``{"share_tag_format": ...}``."""
    return {"share_tag_format": share_tag_format_for(top_name)}


def split_notes(note: Mapping[str, Any] | None) -> tuple[dict[str, Any], dict[str, Any]]:
    """``(legacy, rest)``: the :data:`LEGACY_NOTE_FIELDS` present in ``note``, and every other key.

    Both are fresh dicts in ``note``'s key order; ``None`` or an empty note gives two
    empty dicts.
    """
    legacy: dict[str, Any] = {}
    rest: dict[str, Any] = {}
    for key, value in (note or {}).items():
        (legacy if key in LEGACY_NOTE_FIELDS else rest)[key] = value
    return legacy, rest


def render_v1_tag(acc: Any, rest: Mapping[str, Any]) -> dict[str, Any]:
    """The pre-v2 partitioned tag: 4 keys; a non-finite benefit stays +-inf (FIX-5 defect, kept).

    Moved verbatim from ``accounting._v1_tag``. ``solver_notes`` (``rest``) is appended
    as a 5th key only when non-empty; legacy note fields have no v1 key and are dropped,
    as before.
    """
    reserve = acc.owner_reserve_km
    tag = {
        "shared": True,
        "owner_haulier_id": acc.owner_haulier_id,
        "carrier_haulier_id": acc.carrier_haulier_id,
        "benefit_km": round(reserve - acc.awarded_cost_km, 3) if reserve is not None else None,
    }
    if rest:
        tag[SOLVER_NOTES_FIELD] = dict(rest)
    return tag


def render_audited_tag(acc: Any, legacy: Mapping[str, Any], rest: Mapping[str, Any]) -> dict[str, Any]:
    """The pooled tag: 8 keys, no non-finite number, ``benefit = reserve - awarded``.

    Moved verbatim from ``accounting._audited_tag``: the same 8 keys in the same order
    (``pool_id`` 5th, ``market_round`` 8th, both from ``legacy``, ``None`` when absent).
    ``solver_notes`` (``rest``) is appended LAST only when non-empty.
    """
    from apps.container_logistics.assignment.accounting import finite_or_none

    reserve = acc.owner_reserve_km
    awarded = finite_or_none(acc.awarded_cost_km)
    benefit = None
    if reserve is not None and awarded is not None:
        benefit = finite_or_none(reserve - awarded)
    tag = {
        "shared": True,
        "owner_haulier_id": acc.owner_haulier_id,
        "carrier_haulier_id": acc.carrier_haulier_id,
        # SIGNED on purpose (F3): a cost-blind or cap-limited solver can genuinely pick
        # a worse-than-own partner truck. None = no free feasible own truck.
        "benefit_km": round(benefit, 3) if benefit is not None else None,
        "pool_id": legacy.get("pool_id"),
        "awarded_cost_km": round(awarded, 3) if awarded is not None else None,
        "owner_reserve_km": round(reserve, 3) if reserve is not None else None,
        "market_round": legacy.get("market_round"),
    }
    if rest:
        tag[SOLVER_NOTES_FIELD] = dict(rest)
    return tag


def _v1(acc: Any, note: Mapping[str, Any] | None) -> dict[str, Any]:
    return render_v1_tag(acc, split_notes(note)[1])


def _audited(acc: Any, note: Mapping[str, Any] | None) -> dict[str, Any]:
    return render_audited_tag(acc, *split_notes(note))


_RENDERERS: dict[str, Callable[[Any, Mapping[str, Any] | None], dict[str, Any]]] = {
    "v1": _v1,
    "audited": _audited,
}


def share_tag_renderer(fmt: str) -> Callable[[Any, Mapping[str, Any] | None], dict[str, Any]]:
    """``render(account, note) -> tag`` for the share-tag format ``fmt``.

    ``fmt="v1"``: :func:`render_v1_tag`; ``fmt="audited"``: :func:`render_audited_tag`;
    ``note`` is split by :func:`split_notes` first. Raises ``ValueError`` for another
    ``fmt`` (message unchanged from ``accounting.render_share_tags``).
    """
    try:
        return _RENDERERS[fmt]
    except (KeyError, TypeError):
        raise ValueError(f"share tag format must be 'v1' or 'audited' (got {fmt!r}).") from None


def wire_fields(tag: Mapping[str, Any]) -> dict[str, Any]:
    """The ``AssignedHaulTripPayload`` keywords for one share tag (``{}`` for an unshared pair).

    Exactly what ``publish()`` passed before (``shared`` -> ``bool(tag.get("shared",
    False))``, every other field ``tag.get(k)``), plus ``solver_notes`` only when the
    tag carries it. Keyword order is immaterial to the bytes: the payload's ``__dict__``
    follows the dataclass FIELD order.
    """
    fields: dict[str, Any] = {"shared": bool(tag.get("shared", False))}
    for key in _TAG_WIRE_FIELDS:
        fields[key] = tag.get(key)
    if SOLVER_NOTES_FIELD in tag:
        fields[SOLVER_NOTES_FIELD] = tag[SOLVER_NOTES_FIELD]
    return fields
