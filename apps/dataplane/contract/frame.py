"""The Frame wire contract: binary codec, JSON twin, and the state-code table.

Every other module in apps.dataplane (hot store, duck store, mongo archive,
ingest, supervisor) imports this module and only this module for the frame
shape. Nothing here may import numpy-adjacent heavy deps beyond numpy/orjson;
stdlib only for time handling.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from datetime import datetime, timezone

import numpy as np
import orjson

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

FRAME_MAGIC = b"ORDP"
FRAME_VERSION = 1
HEADER_SIZE = 24
KIND_KEYFRAME = 0
KIND_DELTA = 1

# magic, version, kind, reserved, frame_idx, n, sim_time_ms
HEADER_STRUCT = struct.Struct("<4sHBBIId")
assert HEADER_STRUCT.size == HEADER_SIZE

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class FrameError(Exception):
    """Base error for all Frame contract failures."""


class FrameVersionMismatch(FrameError):
    def __init__(self, expected: int, got: int):
        self.expected = expected
        self.got = got
        super().__init__(f"frame version mismatch: expected {expected}, got {got}")


class FrameDecodeError(FrameError):
    """Raised for any structural decode failure (bad magic, bad kind, bad length)."""


# ---------------------------------------------------------------------------
# State-code table — fixed, versioned with the frame, never reordered.
# ---------------------------------------------------------------------------

STATE_NAMES = (
    "unknown",
    "idle",
    "created",
    "assigned",
    "repositioning_to_pickup",
    "queued_for_pickup",
    "at_pickup_gate",
    "loaded_in_transit",
    "queued_for_dropoff",
    "at_dropoff_gate",
    "completed",
    "cancelled",
)

STATE_CODES = {name: idx for idx, name in enumerate(STATE_NAMES)}


def state_code(name: "str | int | None") -> int:
    """Map a state name (or passthrough int code) to its fixed integer code.

    Unknown/None/'' or any unrecognised name falls back to 0 ("unknown").
    Match is case-sensitive and exact.
    """
    if name is None:
        return 0
    if isinstance(name, int):
        if 0 <= name < len(STATE_NAMES):
            return name
        return 0
    if isinstance(name, str):
        return STATE_CODES.get(name, 0)
    return 0


def state_name(code: int) -> str:
    """Map an integer state code back to its name; out-of-range codes -> 'unknown'."""
    if 0 <= code < len(STATE_NAMES):
        return STATE_NAMES[code]
    return "unknown"


# ---------------------------------------------------------------------------
# sim_time_ms helper
# ---------------------------------------------------------------------------


def sim_time_ms_from_iso(value) -> float:
    """Convert an ISO-8601 string, datetime, or number to UTC epoch milliseconds.

    Accepts:
      - str: ISO-8601, optionally with trailing 'Z', optionally with microseconds.
      - datetime: naive datetimes are treated as UTC.
      - int/float: passed through as float (already epoch ms).

    Raises ValueError for anything else / unparseable strings.
    """
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, datetime):
        dt = value
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        return dt.timestamp() * 1000.0
    if isinstance(value, str):
        s = value.strip()
        if s.endswith("Z") or s.endswith("z"):
            s = s[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(s)
        except ValueError as exc:
            raise ValueError(f"unparseable ISO-8601 datetime: {value!r}") from exc
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        return dt.timestamp() * 1000.0
    raise ValueError(f"cannot convert {type(value)!r} to sim_time_ms")


# ---------------------------------------------------------------------------
# Frame dataclass
# ---------------------------------------------------------------------------


@dataclass(eq=False)
class Frame:
    frame_idx: int
    sim_time_ms: float
    lng: np.ndarray  # float64, C-contiguous, shape (n,)
    lat: np.ndarray  # float64
    slot: np.ndarray  # uint32
    state: np.ndarray  # uint8
    haulier: np.ndarray  # uint8
    kind: int = KIND_KEYFRAME
    version: int = FRAME_VERSION

    def __post_init__(self) -> None:
        self.lng = np.ascontiguousarray(self.lng, dtype=np.float64)
        self.lat = np.ascontiguousarray(self.lat, dtype=np.float64)
        self.slot = np.ascontiguousarray(self.slot, dtype=np.uint32)
        self.state = np.ascontiguousarray(self.state, dtype=np.uint8)
        self.haulier = np.ascontiguousarray(self.haulier, dtype=np.uint8)
        lengths = {
            len(self.lng),
            len(self.lat),
            len(self.slot),
            len(self.state),
            len(self.haulier),
        }
        if len(lengths) != 1:
            raise FrameError(
                "Frame arrays must all have the same length, got "
                f"lng={len(self.lng)} lat={len(self.lat)} slot={len(self.slot)} "
                f"state={len(self.state)} haulier={len(self.haulier)}"
            )

    @property
    def n(self) -> int:
        return int(len(self.lng))

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Frame):
            return NotImplemented
        return (
            int(self.version) == int(other.version)
            and int(self.kind) == int(other.kind)
            and int(self.frame_idx) == int(other.frame_idx)
            and self.sim_time_ms == other.sim_time_ms
            and np.array_equal(self.lng, other.lng)
            and np.array_equal(self.lat, other.lat)
            and np.array_equal(self.slot, other.slot)
            and np.array_equal(self.state, other.state)
            and np.array_equal(self.haulier, other.haulier)
        )

    def __repr__(self) -> str:
        return (
            f"Frame(frame_idx={self.frame_idx}, kind={self.kind}, version={self.version}, "
            f"sim_time_ms={self.sim_time_ms}, n={self.n})"
        )


# ---------------------------------------------------------------------------
# Binary codec
# ---------------------------------------------------------------------------


def encode(frame: Frame) -> bytes:
    n = frame.n
    header = HEADER_STRUCT.pack(
        FRAME_MAGIC,
        frame.version,
        frame.kind,
        0,
        frame.frame_idx,
        n,
        frame.sim_time_ms,
    )
    parts = [
        header,
        frame.lng.tobytes(),
        frame.lat.tobytes(),
        frame.slot.tobytes(),
        frame.state.tobytes(),
        frame.haulier.tobytes(),
    ]
    return b"".join(parts)


def decode(buf) -> Frame:
    buf = bytes(buf)
    if len(buf) < HEADER_SIZE:
        raise FrameDecodeError(
            f"buffer too short for header: {len(buf)} < {HEADER_SIZE}"
        )
    magic, version, kind, _reserved, frame_idx, n, sim_time_ms = HEADER_STRUCT.unpack(
        buf[:HEADER_SIZE]
    )
    if magic != FRAME_MAGIC:
        raise FrameDecodeError(f"bad magic: {magic!r}")
    if version != FRAME_VERSION:
        raise FrameVersionMismatch(expected=FRAME_VERSION, got=version)
    if kind not in (KIND_KEYFRAME, KIND_DELTA):
        raise FrameDecodeError(f"bad kind: {kind}")
    expected_len = HEADER_SIZE + 22 * n
    if len(buf) != expected_len:
        raise FrameDecodeError(
            f"buffer length mismatch: expected {expected_len}, got {len(buf)}"
        )

    off = HEADER_SIZE
    lng = np.frombuffer(buf, dtype="<f8", count=n, offset=off).copy()
    off += 8 * n
    lat = np.frombuffer(buf, dtype="<f8", count=n, offset=off).copy()
    off += 8 * n
    slot = np.frombuffer(buf, dtype="<u4", count=n, offset=off).copy()
    off += 4 * n
    state = np.frombuffer(buf, dtype=np.uint8, count=n, offset=off).copy()
    off += n
    haulier = np.frombuffer(buf, dtype=np.uint8, count=n, offset=off).copy()
    off += n

    return Frame(
        frame_idx=frame_idx,
        sim_time_ms=sim_time_ms,
        lng=lng,
        lat=lat,
        slot=slot,
        state=state,
        haulier=haulier,
        kind=kind,
        version=version,
    )


# ---------------------------------------------------------------------------
# JSON twin
# ---------------------------------------------------------------------------


def encode_json(frame: Frame) -> bytes:
    if not np.all(np.isfinite(frame.lng)) or not np.all(np.isfinite(frame.lat)):
        raise FrameError(
            "encode_json: lng/lat must be finite (orjson would silently turn "
            "NaN/inf into null, breaking round-trip)"
        )
    payload = {
        "magic": FRAME_MAGIC.decode("ascii"),
        "version": frame.version,
        "kind": frame.kind,
        "frame_idx": frame.frame_idx,
        "n": frame.n,
        "sim_time_ms": frame.sim_time_ms,
        "lng": frame.lng,
        "lat": frame.lat,
        "slot": frame.slot,
        "state": frame.state,
        "haulier": frame.haulier,
    }
    return orjson.dumps(payload, option=orjson.OPT_SERIALIZE_NUMPY)


def decode_json(buf) -> Frame:
    if isinstance(buf, str):
        buf = buf.encode("utf-8")
    obj = orjson.loads(buf)

    magic = obj.get("magic")
    if magic != FRAME_MAGIC.decode("ascii"):
        raise FrameDecodeError(f"bad magic: {magic!r}")
    version = obj.get("version")
    if version != FRAME_VERSION:
        raise FrameVersionMismatch(expected=FRAME_VERSION, got=version)
    kind = obj.get("kind")
    if kind not in (KIND_KEYFRAME, KIND_DELTA):
        raise FrameDecodeError(f"bad kind: {kind}")

    return Frame(
        frame_idx=obj["frame_idx"],
        sim_time_ms=obj["sim_time_ms"],
        lng=np.array(obj["lng"], dtype=np.float64),
        lat=np.array(obj["lat"], dtype=np.float64),
        slot=np.array(obj["slot"], dtype=np.uint32),
        state=np.array(obj["state"], dtype=np.uint8),
        haulier=np.array(obj["haulier"], dtype=np.uint8),
        kind=kind,
        version=version,
    )
