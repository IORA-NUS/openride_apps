"""The planner slot's default selection: kept for old inputs, named in one place.

Single responsibility: :data:`DEFAULT_PLANNER`, the policy a profile with no
``policies.planner`` (and no legacy key naming one) runs. It is ``Partitioned``
because that is what every pre-v2 bundle meant by "no topology"; changing it would
change the meaning of every legacy bundle, so it lives in the compat quarantine and
the slot table only reads it.

Moved from ``policies/framework/slots.py`` (35b5b92, line 66, ``default="Partitioned"``).

Leaf module: stdlib only (``framework/slots.py`` is stdlib-only and imports it).
"""

from __future__ import annotations

__all__ = ["DEFAULT_PLANNER"]

#: The slot default of ``planner``: per-cooperation-group decomposition whose inner
#: defaults to ``RandomAssignment`` (the pre-v2 no-strategy behaviour). The flat legacy
#: leaf default is ``compat.legacy.LEGACY_LEAF_DEFAULT``.
DEFAULT_PLANNER = "Partitioned"
