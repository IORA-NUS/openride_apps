"""The run-start dry run's inputs from a compiled bundle, cached in a content-checked sidecar.

Single responsibility: give the control-plane dry run (``openride_control.policies``)
the two things it reads from ``scenario.json`` -- the assignment collection and the
KEYS of the bundle's settings -- without parsing the whole bundle on every run start
(round-2 review M-C: 2.6 s for the 203 MB ``consortium_collab_7d`` bundle, against a
200-500 ms target). Each dry run is a fresh subprocess, so the cache is on disk:
``<scenario>/scenario.run_inputs.json`` (:data:`SIDECAR_FILENAME`, gitignored like the
bundle it is derived from).

Never stale, by construction. The sidecar records the SHA-256 of the exact bundle
bytes its payload was taken from, plus the bundle's ``os.stat`` identity (device,
inode, size, mtime_ns, ctime_ns). A read is served from the sidecar only when

- the stat identity is unchanged AND the sidecar was written at least
  :data:`RACY_SECONDS` after the bundle last changed (``ctime``, which no user-space
  call can set back; a later write to the same inode moves it, and the margin covers
  the filesystem's coarse timestamp tick -- git's "racily clean" rule), or
- the bundle's bytes hash to the recorded SHA-256 (about 0.15 s for 203 MB; this is
  the path after a fresh compile, a copy of the folder, or a touched file).

Anything else -- no sidecar, a different format, a different hash, an unreadable or
corrupt sidecar -- parses the bundle (what the dry run always did) and rewrites the
sidecar. A sidecar that cannot be written (read-only folder) is skipped silently: the
cache only ever saves time, it never decides anything.

``scenario compile`` warms the sidecar (:func:`warm_run_inputs`) from the bundle it
just wrote, after checking that the file on disk is that bundle (its ``createdAt``
stamp, unique per compile, is in the file's header); a mismatch writes nothing.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from typing import Any, Mapping

from .scenario_bundle import BUNDLE_FILENAME, bundle_path

__all__ = ["SIDECAR_FILENAME", "SIDECAR_FORMAT", "RACY_SECONDS", "RunInputs",
           "read_run_inputs", "warm_run_inputs", "sidecar_path"]

SIDECAR_FILENAME = "scenario.run_inputs.json"
SIDECAR_FORMAT = 1
#: A sidecar written within this many seconds of the bundle's last change is only
#: trusted after a content hash (the stat identity alone cannot prove a same-tick write
#: did not follow it).
RACY_SECONDS = 2.0
_HEADER_BYTES = 4096


@dataclass(frozen=True)
class RunInputs:
    """``assignment``: the bundle's ``agents.assignment`` collection (``None`` if it has none);
    ``settings_keys``: the keys of the bundle's ``settings``, in order; ``source``: how
    the read was served (``"stat"``, ``"hash"`` or ``"parse"``; for tests and timing)."""

    assignment: dict[str, Any] | None
    settings_keys: tuple[str, ...]
    source: str


def sidecar_path(scenario_dir: str) -> str:
    return os.path.join(scenario_dir, SIDECAR_FILENAME)


def _identity(st: os.stat_result) -> list[int]:
    return [st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns]


def _payload_of(bundle: Mapping[str, Any]) -> tuple[dict[str, Any] | None, tuple[str, ...]]:
    agents = bundle.get("agents")
    assignment = agents.get("assignment") if isinstance(agents, dict) else None
    settings = bundle.get("settings")
    return (assignment if isinstance(assignment, dict) else None,
            tuple(settings) if isinstance(settings, dict) else ())


def _read_sidecar(scenario_dir: str) -> dict[str, Any] | None:
    try:
        with open(sidecar_path(scenario_dir), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("format") != SIDECAR_FORMAT:
        return None
    if not isinstance(data.get("sha256"), str) or not isinstance(data.get("identity"), list):
        return None
    if not (data.get("assignment") is None or isinstance(data.get("assignment"), dict)):
        return None
    if not isinstance(data.get("settings_keys"), list):
        return None
    return data


def _write_sidecar(scenario_dir: str, *, sha256: str, identity: list[int],
                   assignment: dict[str, Any] | None, settings_keys: tuple[str, ...]) -> None:
    """Atomic best-effort write (tmp + replace); any ``OSError`` is swallowed."""
    target = sidecar_path(scenario_dir)
    tmp = f"{target}.{os.getpid()}.tmp"
    record = {"format": SIDECAR_FORMAT, "bundle": BUNDLE_FILENAME, "sha256": sha256,
              "identity": identity, "written_ns": time.time_ns(),
              "assignment": assignment, "settings_keys": list(settings_keys)}
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(record, fh)
        os.replace(tmp, target)
    except (OSError, TypeError, ValueError):
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _served(data: Mapping[str, Any], source: str) -> RunInputs:
    return RunInputs(assignment=data["assignment"], settings_keys=tuple(data["settings_keys"]),
                     source=source)


def read_run_inputs(scenario_dir: str) -> RunInputs | None:
    """The bundle's run inputs (module docstring); ``None`` when there is no readable bundle.

    ``None`` exactly where ``scenario_bundle.read_bundle`` returns ``None`` (absent, not
    a JSON object, unreadable), so the caller's fallbacks are unchanged.
    """
    path = bundle_path(scenario_dir)
    try:
        st = os.stat(path)
    except OSError:
        return None
    sidecar = _read_sidecar(scenario_dir)
    if sidecar is not None and sidecar["identity"] == _identity(st):
        written = sidecar.get("written_ns")
        if isinstance(written, int) and written - st.st_ctime_ns >= RACY_SECONDS * 1e9:
            return _served(sidecar, "stat")
    try:
        with open(path, "rb") as fh:
            before = os.fstat(fh.fileno())
            raw = fh.read()
            after = os.fstat(fh.fileno())
    except OSError:
        return None
    digest = hashlib.sha256(raw).hexdigest()
    stable = _identity(before) == _identity(after)
    if sidecar is not None and sidecar["sha256"] == digest:
        if stable:
            _write_sidecar(scenario_dir, sha256=digest, identity=_identity(after),
                           assignment=sidecar["assignment"], settings_keys=tuple(sidecar["settings_keys"]))
        return _served(sidecar, "hash")
    try:
        bundle = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(bundle, dict):
        return None
    assignment, keys = _payload_of(bundle)
    if stable:
        _write_sidecar(scenario_dir, sha256=digest, identity=_identity(after),
                       assignment=assignment, settings_keys=keys)
    return RunInputs(assignment=assignment, settings_keys=keys, source="parse")


def warm_run_inputs(scenario_dir: str, bundle: Mapping[str, Any]) -> bool:
    """Write the sidecar for the bundle ``scenario compile`` just wrote; ``True`` if written.

    Hashes the bytes on disk and checks that the file is ``bundle``: its header (first
    4 KB) carries ``bundle``'s own ``"createdAt"`` value, which is unique per compile
    (``compile_bundle``). A file someone else replaced it with does not carry it, and
    nothing is written. Best-effort; never raises for I/O.
    """
    created = bundle.get("createdAt")
    if not isinstance(created, str) or not created:
        return False
    try:
        with open(bundle_path(scenario_dir), "rb") as fh:
            before = os.fstat(fh.fileno())
            raw = fh.read()
            after = os.fstat(fh.fileno())
    except OSError:
        return False
    if _identity(before) != _identity(after):
        return False
    if f'"createdAt": {json.dumps(created)}'.encode() not in raw[:_HEADER_BYTES]:
        return False
    assignment, keys = _payload_of(bundle)
    _write_sidecar(scenario_dir, sha256=hashlib.sha256(raw).hexdigest(), identity=_identity(after),
                   assignment=json.loads(json.dumps(assignment)) if assignment is not None else None,
                   settings_keys=keys)
    return os.path.isfile(sidecar_path(scenario_dir))
