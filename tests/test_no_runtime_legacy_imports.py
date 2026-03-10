from pathlib import Path


LEGACY_IMPORT_SNIPPETS = (
    "apps.driver_app",
    "apps.passenger_app",
    "apps.assignment_app",
    "apps.analytics_app",
)

ALLOWED_PATH_PARTS = {
    "apps/driver_app/",
    "apps/passenger_app/",
    "apps/assignment_app/",
    "apps/analytics_app/",
}


def test_runtime_code_does_not_import_legacy_package_paths():
    repo_root = Path(__file__).resolve().parents[1]
    apps_root = repo_root / "apps"
    offenders = []

    for py_file in apps_root.rglob("*.py"):
        rel = py_file.relative_to(repo_root).as_posix()

        # Legacy shim packages are allowed to reference legacy names by design.
        if any(part in rel for part in ALLOWED_PATH_PARTS):
            continue

        contents = py_file.read_text(encoding="utf-8")
        for snippet in LEGACY_IMPORT_SNIPPETS:
            if snippet in contents:
                offenders.append(f"{rel}: contains '{snippet}'")

    assert offenders == [], "\n".join(offenders)
