"""``RoundStatsAggregator``: the pooled market's per-run round provenance (``run_config.meta.market``).

Single responsibility: fold each tick's market notes (``PooledMarket``'s
``Result.tick_notes``: ``rounds_used``, ``converged``, ``candidate_pairs_round1``,
``candidate_pairs_total``, ``max_rounds``) into run-level round provenance, decide when
it is pushed, and say loudly when the auction was truncated by its backstop. A
``dispatch.run_stats.RunStatsAggregator`` with ``meta_key = "market"``, declared by
``PooledMarket.bind`` at the top (plan ``plan_contract_final.md`` §2.8, WS4a).

Moved verbatim from ``assignment/app.py`` at 1e80917: ``round_stats`` (now
:meth:`RoundStatsAggregator.snapshot`), ``_record_round_stats`` (:meth:`fold`; its
forced push is now the ``True`` return), the cadence ``_ROUND_STATS_PATCH_EVERY`` and
the "grew max" rule of ``_patch_round_stats_to_run_config`` (:meth:`push_due` /
:meth:`mark_pushed`). ``clock.horizon_steps`` replaces ``app._sim_horizon_steps`` (the
same value: ``_dispatch_tick`` builds the clock from it). The snapshot bytes are pinned
by the golden pooled cases (``tests/golden/harness.py`` reads them through
``compat.run_stats.LegacyAppAccessors.round_stats``).

``_``-prefixed so ``framework.registry.discover_builtin_policies`` does not import it as
a policy module. Leaf module: stdlib only. Policy side of the boundary.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

__all__ = ["RoundStatsAggregator", "ROUND_STATS_PATCH_EVERY"]

# How often the pooled market's round provenance is pushed onto run_config.
# Cheap (two HTTP calls, a few dozen times per run) and bounded: the recorded
# stats can lag the true tick count by at most this many ticks, but a truncation
# event is pushed immediately, so `ticks_truncated_by_backstop` is never stale.
ROUND_STATS_PATCH_EVERY = 5


class RoundStatsAggregator:
    """Per-run round provenance for the pooled market (plan §14.4 R3-5/R3-6).

    ``ticks_truncated_by_backstop > 0`` means the auction was cut short on at
    least one tick, so that tick under-served and the run's throughput and
    deadhead numbers are suspect. A trustworthy cooperation result needs 0.
    """

    meta_key = "market"

    def __init__(self) -> None:
        self._s: dict[str, Any] | None = None

    @classmethod
    def empty_snapshot(cls) -> dict[str, Any]:
        """The provenance before any tick (``app.round_stats()`` on a run that never ran a market)."""
        return {
            "ticks": 0, "ticks_in_horizon": 0, "ticks_drain": 0,
            "ticks_idle": 0, "auction_ticks": 0,
            "rounds_min": None, "rounds_mean": None, "rounds_max": None,
            "ticks_truncated_by_backstop": 0, "max_rounds_backstop": None,
            "candidate_pairs_round1": 0, "candidate_pairs_total": 0,
        }

    def snapshot(self) -> dict[str, Any]:
        """The run's round provenance so far.

        Two round-2 corrections are baked in here:

        * ``rounds_mean`` averages over **auction ticks only** (``rounds_used >= 1``).
          Averaging in the no-op ticks -- where the market opened with nothing free to
          allocate -- diluted the figure that is quoted as *auction depth* and made a
          deeper auction look shallower (review MEDIUM-9). Idle ticks are still
          reported, as ``ticks_idle``, because losing them would hide the opposite
          error.
        * ``ticks`` is split into ``ticks_in_horizon`` / ``ticks_drain`` so the count
          sits on the same horizon as the KPI block beside it (review HIGH-4).
        """
        s = self._s
        if not s or not s.get("ticks"):
            return self.empty_snapshot()
        auction = s["auction_ticks"]
        return {
            "ticks": s["ticks"],
            "ticks_in_horizon": s["ticks_in_horizon"],
            "ticks_drain": s["ticks_drain"],
            "ticks_idle": s["ticks_idle"],
            "auction_ticks": auction,
            # min/mean over ticks where an auction actually ran; max over all.
            "rounds_min": s["rounds_min"],
            "rounds_mean": (round(s["rounds_sum"] / auction, 3) if auction else None),
            "rounds_max": s["rounds_max"],
            "ticks_truncated_by_backstop": s["truncated"],
            "max_rounds_backstop": s.get("backstop"),
            "candidate_pairs_round1": s["cand_round1"],
            "candidate_pairs_total": s["cand_total"],
        }

    def push_due(self, *, force: bool = False) -> bool:
        """Push on a schedule, and additionally whenever the round profile grows.

        A new maximum is new information and there may be few ticks in total, so a pure
        modulo cadence can leave the final snapshot stale. Nothing is due before the
        first tick, forced or not.
        """
        stats = self._s
        if not stats or not stats.get("ticks"):
            return False
        if force:
            return True
        grew = stats.get("rounds_max") != stats.get("_last_pushed_max")
        return bool(grew or not stats["ticks"] % ROUND_STATS_PATCH_EVERY)

    def mark_pushed(self) -> None:
        self._s["_last_pushed_max"] = self._s.get("rounds_max")

    def fold(self, tick_notes: Mapping[str, Any], clock: Any) -> bool:
        """Fold one tick's market provenance; ``True`` (push now) when the auction was truncated."""
        s = self._s
        if s is None:
            s = {"ticks": 0, "ticks_in_horizon": 0, "ticks_drain": 0, "ticks_idle": 0,
                 "auction_ticks": 0, "rounds_sum": 0, "rounds_min": None,
                 "rounds_max": None, "truncated": 0, "backstop": None, "warned": False,
                 "cand_round1": 0, "cand_total": 0}
            self._s = s
        market_notes = tick_notes
        r = int(market_notes["rounds_used"])
        # Candidate provenance (plan §14.4 R3-2): round-1 parity with `partitioned`
        # is the guarantee; the per-tick total legitimately exceeds it in a
        # multi-round auction. Recording both makes the ratio auditable instead of
        # arguable.
        s["cand_round1"] += int(market_notes.get("candidate_pairs_round1", 0) or 0)
        s["cand_total"] += int(market_notes.get("candidate_pairs_total", 0) or 0)
        if market_notes.get("max_rounds") is not None:
            s["backstop"] = int(market_notes["max_rounds"])
        s["ticks"] += 1

        # Horizon split: a tick past the simulation horizon is post-horizon DRAIN,
        # not part of the measured window the KPI block covers.
        horizon = getattr(clock, "horizon_steps", None)
        time_step = getattr(clock, "time_step", None)
        if horizon and time_step is not None and int(time_step) >= int(horizon):
            s["ticks_drain"] += 1
        else:
            s["ticks_in_horizon"] += 1

        if r >= 1:
            s["auction_ticks"] += 1
            s["rounds_sum"] += r
            s["rounds_min"] = r if s["rounds_min"] is None else min(s["rounds_min"], r)
        else:
            # The market opened with nothing free to allocate. Real, but not depth.
            s["ticks_idle"] += 1
        s["rounds_max"] = r if s["rounds_max"] is None else max(s["rounds_max"], r)

        if market_notes["converged"]:
            return False
        s["truncated"] += 1
        if not s["warned"]:
            # Loud, once, and greppable in celery_log.txt -- agent-side logs do
            # not reach simulation_log.txt (CLAUDE.md §8).
            logging.error(
                "POOLED MARKET TRUNCATED BY BACKSTOP on tick %d (rounds_used=%d). "
                "The auction had not converged; this run under-serves and its "
                "cooperation numbers are NOT trustworthy. Raise "
                "planner.market.max_rounds.",
                s["ticks"], r,
            )
            s["warned"] = True
        # Push immediately: truncation is the one condition that must never be
        # silent, and the run may end before the next scheduled patch.
        return True
