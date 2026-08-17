"""MQTT topic naming for the order-lifecycle service.

Import-light on purpose (no orsim, no pymongo, no statemachine): the *emitter* side
(``TruckTripManager``) and the *applier* side (``OrderLifecycleApp``) both need the
topic, and the emitter must not drag the lifecycle service's dependencies into the
truck agent.
"""

from __future__ import annotations

# Concrete (non-wildcard) shared topic suffix. ORSim dispatches app-topic messages by
# exact-match dict lookup on ``f"{run_id}/{manager.get_id()}"``, so the lifecycle
# manager's resource ``_id`` must equal this value for the subscription to line up.
ORDER_LIFECYCLE_TOPIC_SUFFIX = "order_lifecycle"


def order_lifecycle_topic(run_id: str) -> str:
    """The one topic every ORDER_* workflow event is published to in ``service`` mode."""
    return f"{run_id}/{ORDER_LIFECYCLE_TOPIC_SUFFIX}"
