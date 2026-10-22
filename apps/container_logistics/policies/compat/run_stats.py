"""``LegacyAppAccessors``: the old ``AssignmentApp.round_stats()`` accessor, kept for its callers.

Single responsibility: keep ``AssignmentApp.round_stats()`` -- the pooled market's run
provenance as one dict -- working after WS4a moved the round stats out of the app into
the market's own run-stats aggregator (``dispatch/library/_market_stats.py``; plan
``plan_contract_final.md`` §5.1 row 19, §7.8). The frozen golden harness calls it
(``tests/golden/harness.py``), as do the round-stats tests.

Why compat: it names one solver's run-stats key (``market``) on the app, i.e. it
translates an old accessor onto the generic ``app._run_stats`` store (meta key ->
aggregator, filled by ``dispatch.run_stats.record_tick``).

Leaf module: stdlib only at import; the aggregator class is imported inside the method
(it lives in ``dispatch.library``, whose package discovery must stay off this path).
"""

from __future__ import annotations

from typing import Any, Dict

__all__ = ["LegacyAppAccessors"]


class LegacyAppAccessors:
    """Mixin for ``AssignmentApp``: old accessors over the generic run-stats store."""

    def round_stats(self) -> Dict[str, Any]:
        """The pooled market's round provenance (an empty snapshot when no market ran)."""
        agg = (getattr(self, "_run_stats", None) or {}).get("market")
        if agg is not None:
            return agg.snapshot()
        from apps.container_logistics.policies.dispatch.library._market_stats import RoundStatsAggregator

        return RoundStatsAggregator.empty_snapshot()
