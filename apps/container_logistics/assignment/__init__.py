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

def __getattr__(name):
    # Plain function-local imports (not importlib): the framework guards follow static
    # imports, including local ones, so the package stays inside tier A's scanned closure.
    if name == "AssignmentAgent":
        from .agent import AssignmentAgent as value
    elif name == "AssignmentApp":
        from .app import AssignmentApp as value
    elif name == "AssignmentManager":
        from .manager import AssignmentManager as value
    else:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    globals()[name] = value
    return value
