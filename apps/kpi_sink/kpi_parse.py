"""Parse KPI sim_clock values and normalize metric payloads."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from apps.utils.utils import str_to_time


def parse_sim_clock(raw: Any) -> datetime:
    if isinstance(raw, datetime):
        return raw
    if not isinstance(raw, str):
        raise ValueError(f"sim_clock must be str or datetime, got {type(raw)!r}")
    try:
        return str_to_time(raw)
    except ValueError:
        from dateutil import parser as date_parser

        return date_parser.parse(raw)


def coerce_metric_value(raw: Any) -> float:
    if raw is None:
        return 0.0
    return float(raw)
