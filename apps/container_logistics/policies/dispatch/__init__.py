"""The dispatch slot: which truck serves which order, decided once per scope per tick.

Contract, context and runner land in P1 (plan §3). ``library/`` holds new dispatch
policies and is walked by ``framework.registry.discover_builtin_policies``; the two
built-ins stay in ``assignment/solver/`` (plan §1.2 item 5). Leaf package: see the
import-cycle rule in ``apps.container_logistics.policies``. Imports nothing.
"""

__all__: list[str] = []
