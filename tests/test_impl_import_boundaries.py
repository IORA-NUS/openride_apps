import ast
from pathlib import Path


ALLOWED_IMPL_IMPORTERS = {
    "apps/ride_hail/driver/app.py",
    "apps/ride_hail/driver/agent.py",
    "apps/ride_hail/driver/manager.py",
    "apps/ride_hail/driver/trip_manager.py",
    "apps/ride_hail/passenger/app.py",
    "apps/ride_hail/passenger/agent.py",
    "apps/ride_hail/passenger/manager.py",
    "apps/ride_hail/passenger/trip_manager.py",
    "apps/ride_hail/assignment/app.py",
    "apps/ride_hail/assignment/agent.py",
    "apps/ride_hail/assignment/manager.py",
    "apps/ride_hail/analytics/app.py",
    "apps/ride_hail/analytics/agent.py",
    "apps/driver_app/driver_app.py",
    "apps/driver_app/driver_agent_indie.py",
    "apps/driver_app/driver_manager.py",
    "apps/driver_app/driver_trip_manager.py",
    "apps/passenger_app/passenger_app.py",
    "apps/passenger_app/passenger_agent_indie.py",
    "apps/passenger_app/passenger_manager.py",
    "apps/passenger_app/passenger_trip_manager.py",
    "apps/assignment_app/assignment_app.py",
    "apps/assignment_app/assignment_agent_indie.py",
    "apps/assignment_app/engine_manager.py",
    "apps/analytics_app/analytics_app.py",
    "apps/analytics_app/analytics_agent_indie.py",
}


def _imports_impl_module(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module.endswith("_impl"):
                return True
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[-1].endswith("_impl"):
                    return True
    return False


def test_impl_module_imports_are_limited_to_compatibility_layers():
    repo_root = Path(__file__).resolve().parents[1]
    apps_root = repo_root / "apps"
    offenders = []

    for py_file in apps_root.rglob("*.py"):
        rel = py_file.relative_to(repo_root).as_posix()
        contents = py_file.read_text(encoding="utf-8")
        tree = ast.parse(contents)

        if _imports_impl_module(tree) and rel not in ALLOWED_IMPL_IMPORTERS:
            offenders.append(rel)

    assert offenders == [], "Unexpected *_impl importers:\n" + "\n".join(offenders)
