"""Provision the order-lifecycle service's owner user — with role ``admin`` — up front.

Why this exists (it is not defensive boilerplate; it closes a real race):

``ORSimRuntime.__init__`` dispatches the bootstrap celery agents, so the lifecycle agent's
``UserRegistry(role="admin")`` signup can run *concurrently* with ``on_before_run`` ->
``precreate``, which also resolves/creates users. Eve bakes the role into the JWT at login
(``user_identity_loader``) and ``check_auth`` trusts the token's role for its whole life —
there is no per-request DB lookup. So whoever creates ``order_lifecycle_main@test.com`` first
decides the token role, and two branches are silently catastrophic:

* precreate wins with the default ``client`` role -> the agent logs into an existing user and
  gets a **client** token -> ``truck/trip/open_order_ids`` (admin-only aggregation) 401s ->
  ``_try_aggregate_items`` disables aggregates -> the owner-filtered paged fallback sees no
  haul trips -> ``order_ids_with_open_haul()`` returns an empty set forever -> the overdue
  sweep cancels orders that have in-flight hauls, resurrecting exactly the ~6% cancel race
  this design exists to remove.
* the two signups interleave -> the loser 422s on the unique-email index -> ``UserRegistry``
  raises "Bad Credentials" -> the agent fails during init, never creates its app, never
  subscribes to its topic, and nothing surfaces the failure -> ``publish_due`` never runs, so
  the run produces zero demand and zero hauls while looking merely "quiet".

The fix is to remove the race rather than survive it: the sim subprocess provisions the owner
**before** ``SimulationRuntime``/``ORSimRuntime`` is constructed, i.e. before any bootstrap
task can be dispatched, so by the time the agent logs in the user already exists with role
``admin`` and its token is admin from the first request. Idempotent, and self-healing for a
user left as ``client`` by an earlier run (``UserRegistry`` PATCHes the role on mismatch).
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

DEFAULT_OWNER_EMAIL = "order_lifecycle_main@test.com"
DEFAULT_OWNER_PASSWORD = "password"


def owner_credentials(behavior_collection: dict | None) -> tuple[str, str]:
    """``(email, password)`` for the lifecycle owner, from its behavior or the defaults."""
    behavior = next(iter((behavior_collection or {}).values()), None) or {}
    return (
        behavior.get("email") or DEFAULT_OWNER_EMAIL,
        behavior.get("password") or DEFAULT_OWNER_PASSWORD,
    )


def ensure_lifecycle_owner_user(behavior_collection: dict | None, sim_clock: str) -> str:
    """Create (or role-correct) the lifecycle owner as an **admin** user. Returns its email.

    Raises ``RuntimeError`` on failure. Failing the run loudly here is deliberate: every
    silent-failure branch described in the module docstring produces a run that looks fine and
    is wrong, which is strictly worse than not starting.
    """
    from apps.common.user_registry import UserRegistry

    email, password = owner_credentials(behavior_collection)
    try:
        UserRegistry(sim_clock, {"email": email, "password": password}, role="admin")
    except Exception as exc:  # noqa: BLE001 - re-raised with context
        raise RuntimeError(
            f"Could not provision the order-lifecycle owner user {email!r} with role 'admin'. "
            "Service mode cannot run without it (the sweep's open-haul guard needs admin "
            "reads). Is the API up?"
        ) from exc
    logger.info("Order lifecycle: owner user %s ready (role=admin).", email)
    return email
