"""Solver-helper library: building blocks a dispatch SOLVER may use; the framework never does.

Single responsibility: the code solvers share (candidate generation, the dispatch
objective, the pair-solver adapter base) without making any of it a framework
concept (plan ``plan_contract_final.md`` decision (f), §6). Built-in and third-party
solvers import from here; ``policies/framework``, the top level of
``policies/dispatch`` and the run-time framework modules never do (the framework
import guard pins it).

Leaf package: imports nothing.
"""

__all__: list[str] = []
