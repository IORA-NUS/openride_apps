"""Day-plan time model: how long a planned job occupies its truck (plan D3).

Solver helper (``policies/solver_lib``); the framework never imports it. Pure Python,
deterministic, no I/O: the fitted coefficients live in the GENERATED data module
``time_model_data.py`` (written by ``scripts/fit_day_plan_time_model.py``), which is a
plain Python literal, so importing it reads nothing at run time.

What it predicts (plan D3). A truck can only be paired with an order once the order is
RELEASED (``meta.request_time_step``) and the truck is free, so the empty leg is always
driven after release. For an order released at ``r`` and dispatched at
``r + latency_seconds(cadence, step)``, :meth:`TimeModel.job_seconds` is the predicted
seconds from DISPATCH (award) to haul completion -- i.e. release-to-completion minus the
dispatch latency. It contains the typical empty leg, both facility queues, both service
times and the laden leg, as the baseline run measured them. The plan's job end is

    e_i = r_i + latency_seconds(cadence, step) + job_seconds(features_i)

Features (each a small bucket, so the cell table stays dense):

- pickup / dropoff location type: ``CT`` (port), ``CU`` (customer), ``MT`` (depot /
  yard). The order profile's ``*_location_type`` field is NOT usable -- every order in
  the shipped bundles carries ``"Depo"`` / ``"Warehouse"`` -- so :func:`location_type`
  also accepts a facility name and maps its prefix (``port_002`` -> ``CT``);
- hour bucket of the DISPATCH moment on the ELAPSED-since-REFERENCE_TIME axis (plan
  D5: the same axis as the demand curve and the day boundaries), never wall clock;
- pickup -> dropoff great-circle km bucket.

Estimator: the per-cell MEDIAN (the MAE-optimal point), backed off through coarser cells
(``pair+hour+km`` -> ``pair+hour`` -> ``pair`` -> global) when a cell has fewer than
``min_cell_n`` baseline trips. Medians under a right-skewed queue under-predict the
tail on purpose; the planner absorbs that with ``nf_late_slack_min``, which the fit
recommends from the P90 of the signed hold-out residual (critique minor 14).
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence

__all__ = [
    "LOCATION_TYPES",
    "SCHEMA_VERSION",
    "JobFeatures",
    "TimeModel",
    "TimeModelError",
    "haversine_km",
    "hour_bucket",
    "km_bucket",
    "latency_seconds",
    "location_type",
    "model_digest",
    "validate_model",
]

SCHEMA_VERSION = 1

#: Location-type codes, the facility ``code`` vocabulary of the bundles.
LOCATION_TYPES: tuple[str, ...] = ("CT", "CU", "MT")
#: Unknown location type: still predictable through the backoff to ``global``.
UNKNOWN_TYPE = "??"

_NAME_PREFIX = {
    "port": "CT",
    "terminal": "CT",
    "customer": "CU",
    "depot": "MT",
    "yard": "MT",
}
_TYPE_ALIASES = {
    "ct": "CT",
    "port": "CT",
    "cu": "CU",
    "customer": "CU",
    "mt": "MT",
    "yd": "MT",
    "depot": "MT",
    "yard": "MT",
}

#: The keys a model mapping must carry, and their types (``validate_model``).
_REQUIRED = {
    "schema_version": int,
    "features": dict,
    "job_seconds": dict,
    "deadhead": dict,
}


class TimeModelError(ValueError):
    """The model data is malformed (wrong schema, missing key, bad value)."""


# --------------------------------------------------------------------------- features


def location_type(value: Optional[str]) -> str:
    """Map a facility code, type label or facility NAME to ``CT``/``CU``/``MT``.

    ``"CT"`` -> ``CT``; ``"port_002"`` -> ``CT``; ``"customer_018"`` -> ``CU``;
    ``"depot_039"`` -> ``MT``. Anything else (``"Depo"``, ``"Warehouse"``, ``None``)
    is :data:`UNKNOWN_TYPE`, which the backoff predicts with the global median.
    """
    if not value:
        return UNKNOWN_TYPE
    text = str(value).strip()
    alias = _TYPE_ALIASES.get(text.lower())
    if alias:
        return alias
    prefix = text.split("_", 1)[0].lower()
    return _NAME_PREFIX.get(prefix, UNKNOWN_TYPE)


def hour_bucket(elapsed_seconds: float, bucket_hours: int) -> int:
    """Bucket of the hour-of-run-day on the ELAPSED axis (``0 .. 24/bucket_hours - 1``)."""
    if bucket_hours <= 0 or 24 % bucket_hours:
        raise TimeModelError(f"bucket_hours must divide 24, got {bucket_hours!r}")
    hour = int(math.floor(float(elapsed_seconds) / 3600.0)) % 24
    return hour // bucket_hours


def km_bucket(km: float, edges: Sequence[float]) -> int:
    """Index of the half-open bin ``[edges[i-1], edges[i])``; ``len(edges)`` past the last."""
    value = float(km)
    for index, edge in enumerate(edges):
        if value < edge:
            return index
    return len(edges)


def haversine_km(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    """Great-circle km (R = 6371.0088). Coordinates in GeoJSON order (lon, lat)."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2.0 * 6371.0088 * math.asin(min(1.0, math.sqrt(a)))


def latency_seconds(cadence_steps: int, step_seconds: float) -> float:
    """Expected dispatch latency: half a dispatch cadence (release is uniform in it)."""
    if cadence_steps < 0 or step_seconds < 0:
        raise TimeModelError("cadence_steps and step_seconds must be >= 0")
    return float(cadence_steps) * float(step_seconds) / 2.0


@dataclass(frozen=True)
class JobFeatures:
    """The four model inputs, already bucketed by :meth:`TimeModel.features`."""

    pickup_type: str
    dropoff_type: str
    hour_bucket: int
    km_bucket: int


# ------------------------------------------------------------------------------ model


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), allow_nan=False)


def model_digest(model: Mapping[str, Any]) -> str:
    """``blake2b16:<hex>`` over the canonical JSON of what moves a prediction.

    Covers ``schema_version``, ``features``, ``job_seconds`` and ``deadhead`` -- not the
    fit report (MAE, dates), so re-rendering the same coefficients keeps the digest.
    """
    payload = {key: model[key] for key in ("schema_version", "features", "job_seconds", "deadhead")}
    return "blake2b16:" + hashlib.blake2b(_canonical(payload).encode("utf-8"), digest_size=8).hexdigest()


def validate_model(model: Mapping[str, Any]) -> None:
    """Raise :class:`TimeModelError` unless ``model`` is a well-formed schema-1 model."""
    if not isinstance(model, Mapping):
        raise TimeModelError("time model must be a mapping")
    for key, kind in _REQUIRED.items():
        if key not in model:
            raise TimeModelError(f"time model lacks {key!r}")
        if not isinstance(model[key], kind):
            raise TimeModelError(f"time model {key!r} must be {kind.__name__}")
    if model["schema_version"] != SCHEMA_VERSION:
        raise TimeModelError(
            f"time model schema_version {model['schema_version']!r} != {SCHEMA_VERSION}"
        )
    feats = model["features"]
    hb = feats.get("hour_bucket_hours")
    if not isinstance(hb, int) or hb <= 0 or 24 % hb:
        raise TimeModelError("features.hour_bucket_hours must be a positive divisor of 24")
    edges = feats.get("km_edges")
    if (
        not isinstance(edges, list)
        or not edges
        or any(not isinstance(e, (int, float)) for e in edges)
        or any(b <= a for a, b in zip(edges, edges[1:]))
    ):
        raise TimeModelError("features.km_edges must be a non-empty strictly increasing list")
    if feats.get("hour_axis") != "elapsed":
        raise TimeModelError("features.hour_axis must be 'elapsed' (plan D5)")
    js = model["job_seconds"]
    if not isinstance(js.get("min_cell_n"), int) or js["min_cell_n"] < 1:
        raise TimeModelError("job_seconds.min_cell_n must be an int >= 1")
    glob = js.get("global")
    if not isinstance(glob, (int, float)) or not math.isfinite(glob) or glob < 0:
        raise TimeModelError("job_seconds.global must be a finite number >= 0")
    for level in ("cell", "pair_hour", "pair"):
        table = js.get(level)
        if not isinstance(table, dict):
            raise TimeModelError(f"job_seconds.{level} must be a dict")
        for key, entry in table.items():
            if (
                not isinstance(key, str)
                or not isinstance(entry, list)
                or len(entry) != 2
                or not isinstance(entry[0], (int, float))
                or not math.isfinite(entry[0])
                or entry[0] < 0
                or not isinstance(entry[1], int)
                or entry[1] < 1
            ):
                raise TimeModelError(f"job_seconds.{level}[{key!r}] must be [seconds>=0, n>=1]")
    dh = model["deadhead"]
    for key in ("intercept_s", "per_km_s"):
        value = dh.get(key)
        if not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise TimeModelError(f"deadhead.{key} must be a finite number >= 0")


def _key(*parts: Any) -> str:
    return "|".join(str(p) for p in parts)


class TimeModel:
    """Predictions from one fitted model mapping (see ``time_model_data.MODEL``).

    Immutable after construction; every method is a pure function of its arguments.
    """

    def __init__(self, model: Mapping[str, Any]):
        validate_model(model)
        self._model = json.loads(_canonical(model))  # private deep copy
        feats = self._model["features"]
        self.hour_bucket_hours: int = feats["hour_bucket_hours"]
        self.km_edges: tuple[float, ...] = tuple(float(e) for e in feats["km_edges"])
        js = self._model["job_seconds"]
        self._min_n: int = js["min_cell_n"]
        self._cell: dict[str, list] = js["cell"]
        self._pair_hour: dict[str, list] = js["pair_hour"]
        self._pair: dict[str, list] = js["pair"]
        self._global: float = float(js["global"])
        dh = self._model["deadhead"]
        self._dh_a: float = float(dh["intercept_s"])
        self._dh_b: float = float(dh["per_km_s"])
        self.digest: str = model_digest(self._model)

    # -- construction ------------------------------------------------------------

    @classmethod
    def default(cls) -> "TimeModel":
        """The shipped model (``time_model_data.MODEL``); raises if its digest drifted."""
        from . import time_model_data as data

        model = cls(data.MODEL)
        if model.digest != data.DIGEST:
            raise TimeModelError(
                f"time_model_data.DIGEST {data.DIGEST!r} does not match its MODEL "
                f"({model.digest!r}); regenerate it with scripts/fit_day_plan_time_model.py"
            )
        return model

    @property
    def model(self) -> dict:
        """A deep copy of the model mapping."""
        return json.loads(_canonical(self._model))

    # -- features ----------------------------------------------------------------

    def features(
        self,
        pickup: Optional[str],
        dropoff: Optional[str],
        dispatch_elapsed_s: float,
        km: float,
    ) -> JobFeatures:
        """Bucket raw inputs: location types (code, label or facility name), the
        dispatch moment in seconds since REFERENCE_TIME, and pickup->dropoff km."""
        return JobFeatures(
            location_type(pickup),
            location_type(dropoff),
            hour_bucket(dispatch_elapsed_s, self.hour_bucket_hours),
            km_bucket(km, self.km_edges),
        )

    # -- predictions -------------------------------------------------------------

    def lookup(self, f: JobFeatures) -> tuple[float, str]:
        """``(seconds, level)`` -- the cell median and which backoff level answered."""
        for level, table, key in (
            ("cell", self._cell, _key(f.pickup_type, f.dropoff_type, f.hour_bucket, f.km_bucket)),
            ("pair_hour", self._pair_hour, _key(f.pickup_type, f.dropoff_type, f.hour_bucket)),
            ("pair", self._pair, _key(f.pickup_type, f.dropoff_type)),
        ):
            entry = table.get(key)
            if entry is not None and entry[1] >= self._min_n:
                return float(entry[0]), level
        return self._global, "global"

    def job_seconds(self, f: JobFeatures) -> float:
        """Predicted seconds from dispatch (award) to haul completion."""
        return self.lookup(f)[0]

    def predict(
        self,
        pickup: Optional[str],
        dropoff: Optional[str],
        dispatch_elapsed_s: float,
        km: float,
    ) -> float:
        """:meth:`job_seconds` straight from raw inputs (see :meth:`features`)."""
        return self.job_seconds(self.features(pickup, dropoff, dispatch_elapsed_s, km))

    def deadhead_seconds(self, km: float) -> float:
        """Seconds a truck spends reaching a pickup ``km`` great-circle km away.

        ``intercept + per_km * km`` fitted on the baseline's driven repositioning legs
        (dispatch to pickup-queue arrival, so it includes the truck's own action
        cadence). ``km <= 0`` means no empty leg and returns 0.
        """
        value = float(km)
        if value <= 0.0:
            return 0.0
        return self._dh_a + self._dh_b * value

    def job_end_seconds(
        self,
        release_elapsed_s: float,
        pickup: Optional[str],
        dropoff: Optional[str],
        km: float,
        *,
        cadence_steps: int,
        step_seconds: float,
    ) -> float:
        """``e = r + latency + job_seconds`` on the elapsed axis (plan D3)."""
        dispatch = float(release_elapsed_s) + latency_seconds(cadence_steps, step_seconds)
        return dispatch + self.predict(pickup, dropoff, dispatch, km)
