"""Compatibility quarantine: old inputs and old wire formats, translated at the boundary.

Single responsibility: everything the generic solver contract keeps ONLY so that old
bundles, old run settings, old captures and the old wire/stamp formats still read and
write byte-identically (plan ``plan_contract_final.md`` §0 "Leftovers", §6). It is the
ONLY place framework-side code may name concrete solvers or their vocabulary (pools,
market rounds, topologies, v1 share tags): translating an old input necessarily names
what it translates to.

Guard: ``tests/test_framework_vocabulary.py`` exempts this package entirely, and
``tests/test_framework_imports.py`` lets the framework import from it only an
allow-listed set of ``(module, symbol)`` pairs. Nothing new belongs here unless it
translates something old; a new solver concept belongs to its solver.

Leaf package: this ``__init__`` deliberately imports nothing (sibling modules are added
by parallel workstreams without touching it).
"""

__all__: list[str] = []
