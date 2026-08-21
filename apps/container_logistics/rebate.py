"""Facility rebates — the one pricing rule, shared by settlement and the solver seam.

A facility may publish a time-of-day price schedule. A truck arriving there earns
(or pays) that amount: **priced at arrival, paid at job completion, to the carrier**.
Both of a job's arrivals earn independently — a facility pays whoever shows up, with
no pickup/dropoff role logic, because facilities are not role-divided.

Why this module exists at all (plan §3.3): a schedule that only a hypothetical future
solver consults would be a dead knob — the audit's P11, and ``fifo_queue_policy`` /
``max_queue_size`` are already exactly that on this very profile. The resolution is
that **settlement and the solver seam call the same** :func:`price_at`. The block is
therefore *decision-inert but never read-inert*: every run prices every arrival, with a
rebate-blind solver, forever.

Purity contract (stricter than the plan's, deliberately — see the module note below):
this module imports **stdlib only**. It is pulled into the long-lived Celery analytics
agent and into ``assignment/solver/base.py``, so it must not drag the datagen catalog
(shapely, the address book) behind it, and it must not import ``datagen`` at all —
``datagen/preprocess.py`` imports *this* module for validation, and the reverse edge
would be a package cycle. :class:`RebateCurve`, which the plan §4.2 places here, lives
in ``datagen/distributions/rebate_curve.py`` instead, next to the ``Curve`` it extends.

The hour-of-day trap (plan §G5, and the single most likely way this feature goes
silently wrong): the simulation epoch is **not** midnight and is **not** consistent —
``apps/orsim_config.py`` says 04:00, six other modules and the flagship scenario say
08:00, ``scenario_config.py`` says 00:00. Deriving an hour-of-day from a step index is
therefore 4 or 8 hours wrong depending on which default won. :func:`price_at` parses
the **recorded RFC-1123 timestamp string the run itself wrote** and never accepts a
step index — a bare int/float argument is rejected rather than silently treated as one.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union

logger = logging.getLogger(__name__)

HOURS_PER_DAY = 24

#: The format ``truck/app.py:569`` writes and ``stats.*_queue_arrival_time`` stores.
RFC1123_FORMAT = "%a, %d %b %Y %H:%M:%S GMT"

#: A label, never a unit of account. This codebase has no currency; an unlabelled
#: number is how that becomes unreadable in six months.
DEFAULT_CURRENCY = "credit"

#: ``resolution`` is reserved so a future finer granularity is a schema change rather
#: than a silent reinterpretation of existing data.
SUPPORTED_RESOLUTIONS = ("hour",)

When = Union[str, datetime]


class RebateSpecError(ValueError):
    """Raised for a malformed rebate schedule.

    A ``ValueError`` so ``datagen/preprocess.py`` can re-raise it as the house
    ``SpecValidationError`` (itself a ``ValueError``) without this pure module having
    to import the validation surface.
    """


# --------------------------------------------------------------------------- parse


def _is_real_number(value: Any) -> bool:
    """True for a genuine int/float. ``bool`` is rejected explicitly.

    ``isinstance(True, int)`` is True in Python, so a schedule authored with
    ``"amount": true`` would otherwise price 1.0. Mirrors the house style at
    ``preprocess.py:246`` (``isinstance(rounds, bool)``).
    """
    if isinstance(value, bool):
        return False
    return isinstance(value, (int, float))


def _coerce_amount(value: Any, *, where: str) -> float:
    """A signed, finite amount. Negative is legal; NaN/±inf is not (plan §13 FIX-5)."""
    if not _is_real_number(value):
        raise RebateSpecError(
            f"{where}: 'amount' must be a number (got {value!r} of type "
            f"{type(value).__name__})"
        )
    amount = float(value)
    if math.isnan(amount) or math.isinf(amount):
        raise RebateSpecError(
            f"{where}: 'amount' must be finite (got {value!r}); no non-finite number "
            f"crosses a framework boundary"
        )
    return amount


def _coerce_hour(value: Any, *, where: str) -> int:
    """An integer hour in ``0..23``.

    Deliberately stricter than ``demand._coerce_points``, which silently drops an
    out-of-range hour. A typo'd ``"hour": 24`` must not silently price nothing —
    money gets no silent fallbacks.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise RebateSpecError(
            f"{where}: 'hour' must be an integer 0..23 (got {value!r} of type "
            f"{type(value).__name__})"
        )
    if not 0 <= value <= HOURS_PER_DAY - 1:
        raise RebateSpecError(f"{where}: 'hour' must be in 0..23 (got {value!r})")
    return int(value)


@dataclass(frozen=True)
class RebateSchedule:
    """A dense 24-slot signed price schedule plus its currency label.

    ``amounts[h]`` is what the facility pays a truck arriving in hour ``h`` local to
    the run's own clock. Unlisted hours are **0.0, not interpolated** (plan §4.1):
    interpolating a *price* invents money nobody authored — a schedule reading "pay 20
    at hour 2, pay 20 at hour 22" would, under the demand curve's densification, pay
    ~20 at every hour in between.
    """

    amounts: Tuple[float, ...]
    currency: str = DEFAULT_CURRENCY
    #: Hours the author did not list. They price 0.0; kept so the compile warning and
    #: the provenance stamp can say *which*.
    missing_hours: Tuple[int, ...] = ()

    def __post_init__(self) -> None:
        if len(self.amounts) != HOURS_PER_DAY:
            raise RebateSpecError(
                f"a RebateSchedule holds exactly {HOURS_PER_DAY} amounts "
                f"(got {len(self.amounts)})"
            )

    def value_at(self, hour: int) -> float:
        """The signed amount for an integer hour-of-day."""
        return self.amounts[int(hour) % HOURS_PER_DAY]

    @property
    def is_all_zero(self) -> bool:
        return all(a == 0.0 for a in self.amounts)

    def to_points(self) -> List[Dict[str, Any]]:
        """The dense 24-point authored form.

        Dense rather than sparse on purpose: it is exactly equivalent under the
        zero-gap rule, it makes the compiled artefact self-describing, and it makes
        :meth:`digest` canonical — two different authorings that *mean* the same
        schedule collapse to one digest in the provenance stamp (plan §9).
        """
        return [{"hour": h, "amount": self.amounts[h]} for h in range(HOURS_PER_DAY)]

    def as_block(self) -> Dict[str, Any]:
        """The concrete block stamped onto a compiled facility profile."""
        return {
            "currency": self.currency,
            "resolution": "hour",
            "points": self.to_points(),
        }

    def digest(self) -> str:
        """A stable short digest over (currency, amounts), for the provenance stamp.

        Digest-keyed rather than per-facility because 300 inline copies of the same
        curve is how a stamp becomes unreadable.
        """
        canonical = json.dumps(
            {"currency": self.currency, "amounts": list(self.amounts)},
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.blake2b(canonical.encode("utf-8"), digest_size=8).hexdigest()

    @classmethod
    def zero(cls, currency: str = DEFAULT_CURRENCY) -> "RebateSchedule":
        return cls(amounts=tuple([0.0] * HOURS_PER_DAY), currency=currency)


def parse_rebate_schedule(raw: Any, *, where: str = "rebate") -> RebateSchedule:
    """Validate an authored rebate block and densify it to 24 signed slots.

    **This is deliberately NOT** ``parse_order_demand_curve`` **and must never become
    it** (plan §16.2). Every existing path into ``Curve`` destroys a rebate:
    ``demand._coerce_points:48`` and ``sources.load_curve_file:105`` both clamp with
    ``max(0.0, weight)`` — every surcharge silently becomes zero — and
    ``demand.normalize_hourly_weights:73-76`` divides by the total, turning absolute
    prices into proportions. Only the ``{resolution, points:[{hour, ...}]}`` *shape*
    is shared; the parsing and validation here are new.

    Raises :class:`RebateSpecError` for anything ambiguous; warns (non-fatal) for a
    sparse schedule, naming the missing hours.
    """
    if not isinstance(raw, Mapping):
        raise RebateSpecError(f"{where}: must be an object (got {type(raw).__name__})")

    resolution = raw.get("resolution", "hour")
    if resolution is not None and resolution not in SUPPORTED_RESOLUTIONS:
        raise RebateSpecError(
            f"{where}: unsupported 'resolution' {resolution!r}. "
            f"Available: {list(SUPPORTED_RESOLUTIONS)}"
        )

    currency = raw.get("currency", DEFAULT_CURRENCY)
    if currency is None:
        currency = DEFAULT_CURRENCY
    if not isinstance(currency, str):
        raise RebateSpecError(
            f"{where}: 'currency' must be a string (got {currency!r})"
        )

    points = raw.get("points")
    if points is None or not isinstance(points, (list, tuple)):
        raise RebateSpecError(
            f"{where}: 'points' must be a non-empty list of {{'hour': H, 'amount': A}} "
            f"(got {points!r})"
        )
    if len(points) == 0:
        raise RebateSpecError(
            f"{where}: 'points' is empty. An empty schedule is not a zero schedule — "
            f"omit the 'rebate' block instead, so 'no rebates' and 'rebates silently "
            f"lost' stay distinguishable."
        )

    amounts: Dict[int, float] = {}
    for idx, point in enumerate(points):
        pwhere = f"{where}.points[{idx}]"
        if not isinstance(point, Mapping):
            raise RebateSpecError(f"{pwhere}: must be an object (got {point!r})")
        if "hour" not in point:
            raise RebateSpecError(f"{pwhere}: missing 'hour'")
        hour = _coerce_hour(point.get("hour"), where=pwhere)
        if hour in amounts:
            # Ambiguous authoring resolved by dict order is nondeterminism.
            raise RebateSpecError(
                f"{pwhere}: duplicate 'hour' {hour} — a schedule must name each hour "
                f"at most once"
            )
        has_amount = "amount" in point
        has_weight = "weight" in point
        if has_amount and has_weight:
            # ``weight`` is accepted only as a shape-compatibility alias; carrying both
            # is ambiguous about which is the price.
            raise RebateSpecError(
                f"{pwhere}: carries both 'amount' and 'weight'. Use 'amount' — "
                f"'weight' is only an alias for shape-compatibility with the demand "
                f"curve, and this is money."
            )
        if not (has_amount or has_weight):
            raise RebateSpecError(f"{pwhere}: missing 'amount'")
        amounts[hour] = _coerce_amount(
            point.get("amount") if has_amount else point.get("weight"), where=pwhere
        )

    dense = [amounts.get(h, 0.0) for h in range(HOURS_PER_DAY)]
    missing = tuple(h for h in range(HOURS_PER_DAY) if h not in amounts)
    if missing:
        logger.warning(
            "%s: schedule is sparse — %d of %d hours are unauthored and will price "
            "0.0 (gaps are NOT interpolated, because interpolating a price invents "
            "money nobody authored). Missing hours: %s",
            where, len(missing), HOURS_PER_DAY, list(missing),
        )
    return RebateSchedule(
        amounts=tuple(dense), currency=currency, missing_hours=missing
    )


# ------------------------------------------------------------------------- pricing


_RFC1123_RE = re.compile(
    r"^[A-Za-z]{3},\s+\d{1,2}\s+[A-Za-z]{3}\s+\d{4}\s+\d{2}:\d{2}:\d{2}"
)


def coerce_when(when: When) -> datetime:
    """Parse a recorded arrival stamp into a ``datetime``.

    Accepts the RFC-1123 string the run wrote (``'Wed, 01 Jan 2020 16:00:00 GMT'``) or
    a ``datetime``. **Rejects a bare int/float**: that would be a step index, and
    deriving hour-of-day from a step is the G5 trap — wrong by 4 or 8 hours depending
    on which of the tree's three conflicting ``REFERENCE_TIME`` defaults won. Failing
    loudly here is the entire mitigation.
    """
    if isinstance(when, datetime):
        return when
    if isinstance(when, bool) or isinstance(when, (int, float)):
        raise ValueError(
            f"rebate pricing refuses a numeric time {when!r}: an arrival must be "
            f"priced from the recorded RFC-1123 timestamp, never from a step index "
            f"(the simulation epoch is 04:00 in one module and 08:00 in six others, "
            f"so a step-derived hour is silently 4-8 hours wrong)."
        )
    if not isinstance(when, str) or not when.strip():
        raise ValueError(f"rebate pricing cannot read an arrival time from {when!r}")
    text = when.strip()
    if _RFC1123_RE.match(text):
        try:
            return parsedate_to_datetime(text)
        except (TypeError, ValueError):
            pass
        # ``parsedate_to_datetime`` is strict about the day-of-week matching the date;
        # the sim writes ``strftime`` output, so a mismatch cannot occur, but fall
        # back rather than lose an arrival to a library edge case.
        return datetime.strptime(text, RFC1123_FORMAT)
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(
            f"rebate pricing cannot parse arrival time {when!r} (expected RFC-1123 "
            f"like 'Wed, 01 Jan 2020 16:00:00 GMT')"
        ) from exc


def price_at(schedule: RebateSchedule, when: When) -> float:
    """**The** rebate rule: what this schedule pays an arrival at this time.

    Called by the settlement path on every completed trip in every run, and by the
    solver seam when a solver opts in. One rule, one function — so a solver's
    decision-time estimate and the settled payment can never disagree about the
    *rule*, only about the predicted arrival time (which is the solver's problem: "when
    do I think I will arrive" is a company's belief, i.e. SOLVER by plan §2).
    """
    if schedule is None:
        raise ValueError("price_at requires a RebateSchedule (got None)")
    return schedule.value_at(coerce_when(when).hour)


# ---------------------------------------------------------------------------- book


class RebateBook:
    """``{facility_id -> RebateSchedule}``, shared by settlement and the solver seam."""

    __slots__ = ("_by_facility",)

    def __init__(self, by_facility: Optional[Mapping[str, RebateSchedule]] = None):
        self._by_facility: Dict[str, RebateSchedule] = dict(by_facility or {})

    def __len__(self) -> int:
        return len(self._by_facility)

    def __bool__(self) -> bool:
        return bool(self._by_facility)

    def __contains__(self, facility_id: Any) -> bool:
        return str(facility_id) in self._by_facility

    @property
    def facility_ids(self) -> Tuple[str, ...]:
        return tuple(self._by_facility)

    def schedule_for(self, facility_id: Any) -> Optional[RebateSchedule]:
        if facility_id is None:
            return None
        return self._by_facility.get(str(facility_id))

    @property
    def currency(self) -> Optional[str]:
        """The currency label, or ``None`` when the book is empty.

        Mixed currencies are not modelled (``currency`` is a label, not a unit of
        account); the first schedule's label wins and a mismatch is logged once.
        """
        labels = {s.currency for s in self._by_facility.values()}
        if not labels:
            return None
        if len(labels) > 1:
            logger.warning(
                "rebate book carries mixed currency labels %s; reporting %r. "
                "Currency is a label, not a unit of account — no conversion happens.",
                sorted(labels), sorted(labels)[0],
            )
            return sorted(labels)[0]
        return labels.pop()

    def price_at(self, facility_id: Any, when: When) -> Optional[float]:
        """The signed amount, or ``None`` when this facility publishes no schedule.

        ``None`` means *unpriceable*, which settlement counts rather than imputes.
        An unparseable ``when`` raises — a malformed stamp is a bug worth surfacing,
        and settlement catches it into the same ``unpriced`` counter with a log.
        """
        schedule = self.schedule_for(facility_id)
        if schedule is None:
            return None
        return price_at(schedule, when)

    @classmethod
    def from_facility_docs(cls, docs: Iterable[Mapping[str, Any]]) -> "RebateBook":
        """Build from facility documents (``_id`` + ``profile.rebate``).

        **Id contract (R2-11 / review F14):** keys are ``str(doc["_id"])``, and every
        lookup restringifies, so this is safe for both the REST read (ids arrive as
        strings) and a direct PyMongo read (ids arrive as ``ObjectId``). Do not
        "optimise" either side to use the raw value: an ``ObjectId`` key with a ``str``
        lookup silently misses on EVERY facility, and the symptom is a perfectly
        plausible all-zero ledger rather than an error — the §6.7 silent-empty-read
        shape, applied to money.

        Facilities without a ``rebate`` block are simply absent from the book. A block
        that fails to parse is logged and skipped rather than killing analytics: the
        compile-time gate (plan §5) is the place that rejects bad authoring, and a
        run's reporting should not die because one facility's block is malformed.
        """
        by_facility: Dict[str, RebateSchedule] = {}
        for doc in docs or ():
            if not isinstance(doc, Mapping):
                continue
            fid = doc.get("_id")
            if fid is None:
                continue
            block = (doc.get("profile") or {}).get("rebate")
            if not block:
                continue
            try:
                by_facility[str(fid)] = parse_rebate_schedule(
                    block, where=f"facility[{fid}].profile.rebate"
                )
            except RebateSpecError as exc:
                logger.warning(
                    "facility %s carries an unparseable rebate block (%s) — its "
                    "arrivals will count as unpriced", fid, exc,
                )
        return cls(by_facility)


def schedules_by_digest(
    entries: Sequence[Tuple[str, Any, RebateSchedule]],
) -> Dict[str, Dict[str, Any]]:
    """Collapse ``(facility_name, facility_type, schedule)`` triples by digest.

    The shape the provenance stamp publishes (plan §9): identical schedules collapse,
    and the count plus an example facility make the claim checkable.
    """
    out: Dict[str, Dict[str, Any]] = {}
    for name, ftype, schedule in entries:
        key = schedule.digest()
        bucket = out.get(key)
        if bucket is None:
            bucket = {
                "points": schedule.to_points(),
                "currency": schedule.currency,
                "facility_count": 0,
                "facility_types": set(),
                "example_facility": name,
            }
            out[key] = bucket
        bucket["facility_count"] += 1
        if ftype:
            bucket["facility_types"].add(str(ftype))
    for bucket in out.values():
        bucket["facility_types"] = sorted(bucket["facility_types"])
    return out


# ------------------------------------------------------------------- the epoch rule

#: The ONE simulation-epoch format this codebase writes. Verified 2026-08-21 across all
#: 15 compiled bundles: every one uses this form (13x 08:00:00, 1x 04:00:00, 1x 00:00:00).
CANONICAL_EPOCH_FORMAT = "%Y-%m-%d %H:%M:%S"


def reference_hour(reference_time: Any) -> Optional[int]:
    """Hour-of-day of the simulation epoch — **one rule, one function** (R3-4).

    This exists because there were two independent derivations of the same quantity and
    they disagreed on **4 of 7** realistic epoch spellings. The compiled bundle's
    ``recipe.hour_axis`` and the run record's ``meta.rebate.hour_axis`` are provenance
    for the *same run*, so a disagreement means one artefact can say ``axes_aligned:
    true`` while the other says ``false``. That is the same class of defect
    :func:`price_at` exists to prevent on the pricing side — "one rule, in one function,
    so a solver's estimate and the settled payment can never disagree about the rule".

    Accepts a ``datetime``, or a string in :data:`CANONICAL_EPOCH_FORMAT` **only**.
    Everything else returns ``None``, and ``None`` means *unknown*, never *midnight*:

    * ``'2020-01-01T00:00:00+08:00'`` — an offset makes "which wall clock" ambiguous,
      and the previous permissive parser blessed it as hour 0 / **aligned**, which is
      exactly the confident-but-wrong stamp this feature must not emit.
    * ``'2020-01-01 00:00'`` / ``'2020-01-01'`` — a natural authoring the compiler used
      to accept and bless while the *runtime* could not load it at all.

    Callers that must not proceed on an unknown epoch validate the ``None``
    (``Preprocessor`` refuses the compile); callers that merely describe it stamp the
    ``None`` honestly.
    """
    if isinstance(reference_time, datetime):
        return reference_time.hour
    if not isinstance(reference_time, str) or not reference_time.strip():
        return None
    try:
        return datetime.strptime(reference_time.strip(), CANONICAL_EPOCH_FORMAT).hour
    except ValueError:
        return None
