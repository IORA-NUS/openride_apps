"""R3-8 / review R2-12 — the pre-fix run must SAY it is misaligned.

Two rebate runs are served side by side and are not comparable: `run_20260821_045528`
was compiled on the 08:00 epoch (rebate band on the demand PEAK) and
`run_20260821_055316` on midnight (band in the TROUGH). Nothing in the older record
said so, so a reader would quote it.

The reviewer offered a second option — have the read path refuse to serve rebate fields
for runs lacking `hour_axis`. That is REJECTED (plan §19.5): it is the CLAUDE.md §6.7
silent-empty-read anti-pattern applied to money. A blank panel makes a reader conclude
there is no data, when the truth is that the data is misaligned. **Backfill only.**

Skipped when Mongo is unreachable so the suite stays runnable offline.
"""

import pytest

PRE_FIX_RUN = "run_20260821_045528"
POST_FIX_RUN = "run_20260821_055316"


def _run_config(run_id):
    pymongo = pytest.importorskip("pymongo")
    try:
        client = pymongo.MongoClient("mongodb://localhost:27017/",
                                     serverSelectionTimeoutMS=1500)
        client.admin.command("ping")
    except Exception:
        pytest.skip("MongoDB unreachable")
    doc = client["OpenRoadDB"]["run_config"].find_one({"run_id": run_id})
    if not doc:
        pytest.skip(f"{run_id} not present in this database")
    return (doc.get("meta") or {}).get("rebate") or {}


def test_prefix_run_carries_an_explicit_misaligned_stamp():
    stamp = _run_config(PRE_FIX_RUN)
    axis = stamp.get("hour_axis")
    assert axis, "the pre-fix run has no hour_axis at all — it would be quoted as valid"
    assert axis["axes_aligned"] is False
    assert axis["demand_to_wall_offset_hours"] == 8
    assert axis.get("backfilled") is True, (
        "a backfilled stamp must say it was backfilled, or it is indistinguishable "
        "from one the run actually emitted"
    )
    assert any("EIGHT HOURS APART" in c for c in stamp.get("caveats") or []), (
        "the misalignment must be stated in words beside the flag"
    )


def test_the_postfix_run_is_marked_aligned_and_not_backfilled():
    """The contrast is the point: the two must be distinguishable at a glance."""
    stamp = _run_config(POST_FIX_RUN)
    axis = stamp.get("hour_axis")
    assert axis and axis["axes_aligned"] is True
    assert axis["demand_to_wall_offset_hours"] == 0
    assert "backfilled" not in axis, "the post-fix run emitted its own stamp"


def test_the_read_path_still_serves_the_misaligned_run():
    """Explicitly pinned: the rejected option must stay rejected.

    Refusing to serve rebate fields for an un-stamped/misaligned run would blank the
    panel, and a blank panel reads as 'no data' rather than 'misaligned data'. The run
    is annotated, not withheld.
    """
    pymongo = pytest.importorskip("pymongo")
    try:
        client = pymongo.MongoClient("mongodb://localhost:27017/",
                                     serverSelectionTimeoutMS=1500)
        client.admin.command("ping")
    except Exception:
        pytest.skip("MongoDB unreachable")
    doc = client["OpenRoadDB"]["container_logistics_kpi_breakdown"].find_one(
        {"run_id": PRE_FIX_RUN, "scope": "haulier", "final": True}
    )
    if not doc:
        pytest.skip("pre-fix breakdown not present")
    entities = (doc.get("breakdown") or {}).get("entities") or []
    assert entities, "the misaligned run's rows were withheld, not annotated"
    assert any(e.get("rebate_credited") for e in entities), (
        "rebate fields were blanked for the misaligned run — that is the §6.7 "
        "silent-empty-read anti-pattern, on money"
    )
