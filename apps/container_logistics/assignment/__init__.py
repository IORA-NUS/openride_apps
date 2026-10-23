"""Container logistics assignment package.

The public names are resolved LAZILY (PEP 562), like ``scenario/__init__.py``: importing
a leaf module such as ``assignment.constraints`` or ``assignment.pools`` (the policy
registry's discovery and the control-plane dry run do) must not pull in the agent, and
with it orsim, eventlet and celery (~200 ms per ``validate-run-policies`` subprocess,
round-2 review M-C). ``from apps.container_logistics.assignment import AssignmentApp``
and attribute access work exactly as before.
"""

__all__ = [
    "AssignmentAgent",
    "AssignmentApp",
    "AssignmentManager",
]

_LAZY = {
    "AssignmentAgent": ".agent",
    "AssignmentApp": ".app",
    "AssignmentManager": ".manager",
}


def __getattr__(name):
    module = _LAZY.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    value = getattr(import_module(module, __name__), name)
    globals()[name] = value
    return value
