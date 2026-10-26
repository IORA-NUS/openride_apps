"""Policy slots: every pluggable simulation decision, behind one registry.

Package map (plan ``docs/plans/policy_slots_p0_p3.md`` §3):

- ``framework/`` -- slot table, ``@policy`` registry, typed params, catalog. Leaf code.
- ``dispatch/`` -- the dispatch slot's contract and runner (P1).
- ``dispatch/library/`` -- new dispatch policies; walked by
  ``framework.registry.discover_builtin_policies``.
- ``testing/`` -- synthetic fixtures, the battery and the bench (P3).

Import-cycle rule (plan §2.7): this package ``__init__``, ``framework/*`` and
``dispatch/{base,plan,pair_solver}.py`` are LEAF modules and must never import
``apps.container_logistics.assignment.*`` at module top. ``assignment/__init__``
eagerly imports ``agent`` -> ``app``, and ``app`` imports this package, so such an
import cycles. Where a leaf needs assignment code (e.g. the legacy market registries
in the catalog) it imports locally inside the one function that needs it.

This module deliberately imports nothing.
"""

__all__: list[str] = []
