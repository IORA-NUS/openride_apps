"""``Plan``: re-exported from ``policies.compat.legacy_context`` (where it moved, WS1).

Kept so old importers (``from ...dispatch.plan import Plan``) keep working until the
compat shims are removed (WS8). New solvers return ``dispatch.result.Result``.

Leaf module: imports only the leaf ``compat.legacy_context``.
"""

from __future__ import annotations

from apps.container_logistics.policies.compat.legacy_context import Plan

__all__ = ["Plan"]
