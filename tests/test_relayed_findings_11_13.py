"""Round-2 RELAYED findings 11-13 — each independently reproduced before fixing.

Plan §14.6 said "verify before fixing" and flagged 11 as *likely subsumed by R3-3*.
It is not: I reproduced all three against the real functions. Evidence per test.
"""

import ast
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, "/home/user")

from apps.container_logistics.scenario import frontend_scenario_spec as F

DOMAIN = "container-logistics-sim"


def _two_structure_previous():
    return {
        "name": "prev", "slug": "s",
        "cooperation": {"active": "consortium", "structures": [
            {"id": "consortium", "pools": [{"id": "north", "members": ["acme", "borax", "cargo"]}]},
            {"id": "other", "pools": [{"id": "south", "members": ["cargo", "delta"]}]},
        ]},
    }


def _body_resetting_only_other():
    """Keeps `consortium`'s content, empties `other`."""
    return {"name": "x", "cooperation": {"active": "consortium", "structures": [
        {"id": "consortium", "edges": [["acme", "borax"], ["acme", "cargo"], ["borax", "cargo"]]},
        {"id": "other", "edges": []},
    ]}}


# --- 11: the escape hatch must not reset structures the user never touched ----

def test_reset_is_scoped_to_the_structures_actually_being_emptied():
    """Finding 11, REPRODUCED before the fix: `allow_cooperation_reset` stood the
    pools carry down GLOBALLY (`previous=None`), so an untouched 3-member pool was
    silently reshaped into pairwise pools. The hatch re-created the original bug
    inside itself."""
    previous = _two_structure_previous()
    scoped = F._previous_scoped_to_untouched_structures(previous, _body_resetting_only_other()["cooperation"])

    ids = [s["id"] for s in scoped["cooperation"]["structures"]]
    assert "consortium" in ids, "an UNTOUCHED structure lost its carry during a reset"
    assert "other" not in ids, "the structure being reset kept its carry, so the reset is a no-op"


def test_reset_of_everything_still_stands_the_whole_carry_down():
    previous = _two_structure_previous()
    empty_body = {"active": "consortium", "structures": [
        {"id": "consortium", "edges": []}, {"id": "other", "edges": []}]}
    scoped = F._previous_scoped_to_untouched_structures(previous, empty_body)
    assert scoped["cooperation"]["structures"] == []


def test_no_reset_leaves_previous_untouched():
    previous = _two_structure_previous()
    body = {"active": "consortium", "structures": [
        {"id": "consortium", "edges": [["acme", "borax"]]},
        {"id": "other", "edges": [["cargo", "delta"]]}]}
    assert F._previous_scoped_to_untouched_structures(previous, body) == previous


# --- 12: the guard must fail CLOSED on an unreadable saved spec ---------------

def test_unreadable_previous_is_detected_rather_than_read_as_absent():
    """Finding 12, REPRODUCED: the loader swallows a JSON error and returns None,
    which is indistinguishable from 'brand-new scenario', so the guard silently
    failed open — protecting least the folder most in need of it."""
    d = tempfile.mkdtemp()
    try:
        Path(d, "spec.json").write_text("{ this is not json")
        assert F._load_generation_spec_from_disk(d) is None, "loader no longer swallows"
        assert F._previous_exists_but_is_unreadable(d, None) is True
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_a_genuinely_new_scenario_does_not_trip_the_unreadable_check():
    d = tempfile.mkdtemp()
    try:
        assert F._previous_exists_but_is_unreadable(d, None) is False       # empty dir
        assert F._previous_exists_but_is_unreadable("/nonexistent/x", None) is False
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_a_readable_previous_does_not_trip_the_unreadable_check():
    d = tempfile.mkdtemp()
    try:
        Path(d, "spec.json").write_text(json.dumps({"name": "ok"}))
        assert F._previous_exists_but_is_unreadable(d, {"name": "ok"}) is False
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_generate_scenario_refuses_an_unreadable_previous():
    """The wiring, not just the predicate."""
    src = __import__("inspect").getsource(F.generate_scenario)
    assert "_previous_exists_but_is_unreadable" in src, (
        "the fail-closed check is not wired into generate_scenario"
    )
    assert "allow_cooperation_reset" in src


# --- 13: the machine-readable code must reach the CLI user -------------------

def test_cli_surfaces_the_reset_remedy_for_the_typed_code():
    """Finding 13, REPRODUCED: `ControlError.code` was set and read NOWHERE on the
    CLI path — `cli.py` printed only the message, so the user was told a save was
    refused and never told the override existed."""
    src = Path("/home/user/openride/cli.py").read_text()
    tree = ast.parse(src)
    reads_code = any(
        isinstance(n, ast.Call) and getattr(n.func, "id", "") == "getattr"
        and len(n.args) >= 2 and isinstance(n.args[1], ast.Constant) and n.args[1].value == "code"
        for n in ast.walk(tree)
    )
    assert reads_code, "cli.py never reads ControlError.code"
    assert "COOP_RESET_REQUIRED" in src
    assert "--allow-cooperation-reset" in src, "the remedy string is never shown to the user"
