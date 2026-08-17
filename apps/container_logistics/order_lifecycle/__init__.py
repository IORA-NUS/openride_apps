"""Order-lifecycle service: orders become pure data owned by one service agent.

See ``docs/order_lifecycle_service_plan.md``. Flag-gated by ``ORSIM_ORDER_LIFECYCLE``
(``agents`` = today's per-order agents, the default; ``service`` = this package).
"""

from .agent import OrderLifecycleAgent
from .app import OrderLifecycleApp
from .batch_writer import OrderBatchWriter
from .manager import OrderLifecycleManager
from .topics import ORDER_LIFECYCLE_TOPIC_SUFFIX, order_lifecycle_topic
from .users import ensure_lifecycle_owner_user, owner_credentials

__all__ = [
    "OrderLifecycleAgent",
    "OrderLifecycleApp",
    "OrderLifecycleManager",
    "OrderBatchWriter",
    "order_lifecycle_topic",
    "ORDER_LIFECYCLE_TOPIC_SUFFIX",
    "ensure_lifecycle_owner_user",
    "owner_credentials",
]
