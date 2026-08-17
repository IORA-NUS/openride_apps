"""Container logistics scenario package (lazy imports avoid pulling shapely at import time)."""

__all__ = ["ScenarioManager"]


def __getattr__(name):
    if name == "ScenarioManager":
        from .scenario_manager import ScenarioManager

        return ScenarioManager
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
