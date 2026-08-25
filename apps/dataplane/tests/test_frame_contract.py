"""Tests for apps.dataplane.contract.frame — the wire contract."""

from datetime import datetime, timezone

import numpy as np
import pytest

from apps.dataplane.contract.frame import (
    FRAME_MAGIC,
    FRAME_VERSION,
    HEADER_SIZE,
    HEADER_STRUCT,
    KIND_KEYFRAME,
    Frame,
    FrameDecodeError,
    FrameError,
    FrameVersionMismatch,
    decode,
    decode_json,
    encode,
    encode_json,
    sim_time_ms_from_iso,
    state_code,
    state_name,
    STATE_NAMES,
)


def make_frame(n, frame_idx=0, sim_time_ms=0.0, seed=0):
    rng = np.random.default_rng(seed)
    return Frame(
        frame_idx=frame_idx,
        sim_time_ms=sim_time_ms,
        lng=rng.uniform(3.0, 7.5, n),
        lat=rng.uniform(50.5, 53.5, n),
        slot=np.arange(n, dtype=np.uint32),
        state=rng.integers(0, 12, n, dtype=np.uint8),
        haulier=rng.integers(0, 8, n, dtype=np.uint8),
    )


# ---------------------------------------------------------------------------
# Header layout
# ---------------------------------------------------------------------------


def test_header_is_24_bytes_and_aligned():
    assert HEADER_SIZE == 24
    assert HEADER_STRUCT.size == 24
    # f64 region starts right after the header -> must be 8-byte aligned.
    assert HEADER_SIZE % 8 == 0


def test_encode_length_formula():
    f0 = make_frame(0)
    assert len(encode(f0)) == 24

    for n in (1, 500, 100_000):
        f = make_frame(n, seed=n)
        assert len(encode(f)) == 24 + 22 * n


# ---------------------------------------------------------------------------
# Binary round-trip
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("n", [0, 1, 500, 100_000])
def test_binary_round_trip(n):
    f = make_frame(n, frame_idx=7, sim_time_ms=1234.5, seed=n)
    decoded = decode(encode(f))
    assert decoded == f


@pytest.mark.parametrize("n", [0, 1, 500, 100_000])
def test_json_round_trip(n):
    f = make_frame(n, frame_idx=9, sim_time_ms=98765.25, seed=n + 1)
    decoded = decode_json(encode_json(f))
    assert decoded == f


def test_json_round_trip_exact_float_equality():
    f = make_frame(50, frame_idx=3, sim_time_ms=1717171717.125, seed=42)
    decoded = decode_json(encode_json(f))
    assert np.array_equal(decoded.lng, f.lng)
    assert np.array_equal(decoded.lat, f.lat)
    assert decoded.sim_time_ms == f.sim_time_ms


def test_binary_and_json_encodings_decode_to_equal_frames():
    f = make_frame(37, frame_idx=11, sim_time_ms=42.0, seed=5)
    from_bin = decode(encode(f))
    from_json = decode_json(encode_json(f))
    assert from_bin == from_json


def test_decode_returns_owned_memory():
    f = make_frame(10, seed=1)
    buf = bytearray(encode(f))
    decoded = decode(buf)
    # mutate the source buffer after decode -> decoded arrays must be unaffected
    buf[24] = buf[24] ^ 0xFF
    assert decoded.lng[0] == f.lng[0]


# ---------------------------------------------------------------------------
# Error conditions
# ---------------------------------------------------------------------------


def test_truncated_buffer_raises_decode_error():
    f = make_frame(5, seed=2)
    buf = encode(f)
    with pytest.raises(FrameDecodeError):
        decode(buf[:-1])


def test_short_header_raises_decode_error():
    with pytest.raises(FrameDecodeError):
        decode(b"\x00" * 23)


def test_version_mismatch_raises_with_expected_got():
    f = make_frame(3, seed=3)
    buf = bytearray(encode(f))
    # version is bytes [4:6], little-endian u16 -> bump it
    buf[4] = (FRAME_VERSION + 1) & 0xFF
    with pytest.raises(FrameVersionMismatch) as exc_info:
        decode(bytes(buf))
    assert exc_info.value.expected == FRAME_VERSION
    assert exc_info.value.got == FRAME_VERSION + 1


def test_bad_magic_raises_decode_error():
    f = make_frame(3, seed=4)
    buf = bytearray(encode(f))
    buf[0:4] = b"XXXX"
    with pytest.raises(FrameDecodeError):
        decode(bytes(buf))


def test_bad_kind_raises_decode_error():
    f = make_frame(3, seed=6)
    buf = bytearray(encode(f))
    buf[6] = 99
    with pytest.raises(FrameDecodeError):
        decode(bytes(buf))


def test_mismatched_array_lengths_raise_frame_error():
    with pytest.raises(FrameError):
        Frame(
            frame_idx=0,
            sim_time_ms=0.0,
            lng=np.array([1.0, 2.0]),
            lat=np.array([1.0]),
            slot=np.array([0, 1], dtype=np.uint32),
            state=np.array([0, 0], dtype=np.uint8),
            haulier=np.array([0, 0], dtype=np.uint8),
        )


def test_encode_json_raises_on_nan():
    f = make_frame(4, seed=7)
    f.lng[0] = float("nan")
    with pytest.raises(FrameError):
        encode_json(f)


def test_encode_json_raises_on_inf():
    f = make_frame(4, seed=8)
    f.lat[1] = float("inf")
    with pytest.raises(FrameError):
        encode_json(f)


# ---------------------------------------------------------------------------
# State code table
# ---------------------------------------------------------------------------


def test_state_code_round_trip_for_every_entry():
    for code, name in enumerate(STATE_NAMES):
        assert state_code(name) == code
        assert state_name(code) == name


def test_state_code_unknown_fallback():
    assert state_code(None) == 0
    assert state_code("") == 0
    assert state_code("totally_bogus_state") == 0
    assert state_name(999) == "unknown"
    assert state_name(-1) == "unknown"


def test_state_names_order_pinned():
    assert STATE_NAMES == (
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


# ---------------------------------------------------------------------------
# sim_time_ms_from_iso
# ---------------------------------------------------------------------------


def test_sim_time_ms_from_iso_z_suffix():
    ms = sim_time_ms_from_iso("2026-08-05T12:00:00Z")
    expected = datetime(2026, 8, 5, 12, 0, 0, tzinfo=timezone.utc).timestamp() * 1000.0
    assert ms == expected


def test_sim_time_ms_from_iso_with_micros():
    ms = sim_time_ms_from_iso("2026-08-05T12:00:00.500Z")
    expected = (
        datetime(2026, 8, 5, 12, 0, 0, 500_000, tzinfo=timezone.utc).timestamp()
        * 1000.0
    )
    assert ms == expected


def test_sim_time_ms_from_iso_agrees_with_aware_datetime():
    dt = datetime(2026, 8, 5, 12, 0, 0, 500_000, tzinfo=timezone.utc)
    assert sim_time_ms_from_iso("2026-08-05T12:00:00.500Z") == sim_time_ms_from_iso(dt)


def test_sim_time_ms_from_iso_naive_treated_as_utc():
    naive = datetime(2026, 8, 5, 12, 0, 0)
    aware = datetime(2026, 8, 5, 12, 0, 0, tzinfo=timezone.utc)
    assert sim_time_ms_from_iso(naive) == sim_time_ms_from_iso(aware)


def test_sim_time_ms_from_iso_numeric_passthrough():
    assert sim_time_ms_from_iso(1234.5) == 1234.5
    assert sim_time_ms_from_iso(1234) == 1234.0


def test_sim_time_ms_from_iso_bad_input_raises_value_error():
    with pytest.raises(ValueError):
        sim_time_ms_from_iso("not-a-date")
    with pytest.raises(ValueError):
        sim_time_ms_from_iso(object())


def test_frame_repr_shows_n_not_arrays():
    f = make_frame(100_000, seed=9)
    r = repr(f)
    assert "n=100000" in r
    assert len(r) < 500
