"""Manager for the order-lifecycle service agent.

Subclasses :class:`AssignmentManager` purely to inherit its admin-scoped Eve read helpers —
specifically ``order_ids_with_open_haul()`` (aggregation endpoint + paged fallback), which is
the *only* Eve traffic this service performs. Every order write goes direct to Mongo via
:class:`OrderBatchWriter`.

Like assignment, there is **no Eve resource** behind this manager: ``resource`` is a synthetic
stub whose ``_id`` doubles as the shared MQTT topic suffix (``ORSimApp`` derives
``topic_params`` from ``manager.get_id()``), and ``login``/``logout`` are no-ops.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from apps.container_logistics.assignment.manager import AssignmentManager

from .topics import ORDER_LIFECYCLE_TOPIC_SUFFIX


class OrderLifecycleManager(AssignmentManager):
    def __init__(
        self,
        run_id: str,
        sim_clock: str,
        user,
        profile: Optional[Dict[str, Any]] = None,
        persona: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(
            run_id=run_id,
            sim_clock=sim_clock,
            user=user,
            profile=profile,
            persona=persona,
        )
        # Must equal the topic suffix the truck emitter publishes to (exact-match dispatch).
        self.resource = {"_id": ORDER_LIFECYCLE_TOPIC_SUFFIX, "state": "online"}

    def as_dict(self) -> Dict[str, Any]:
        return {"profile": self.profile, "run_id": self.run_id}
