def _star_imported_names(module_name):
    namespace = {}
    exec(f"from {module_name} import *", {}, namespace)
    return {name for name in namespace if not name.startswith("__")}


def test_ride_hail_star_import_matches_root_all():
    import apps.ridehail as pkg

    assert _star_imported_names("apps.ridehail") == set(pkg.__all__)


def test_ride_hail_adapters_star_import_matches_all():
    import apps.ridehail.adapters as pkg

    assert _star_imported_names("apps.ridehail.adapters") == set(pkg.__all__)


def test_canonical_driver_package_star_import_matches_all():
    import apps.ridehail.driver as pkg

    assert _star_imported_names("apps.ridehail.driver") == set(pkg.__all__)


def test_canonical_passenger_package_star_import_matches_all():
    import apps.ridehail.passenger as pkg

    assert _star_imported_names("apps.ridehail.passenger") == set(pkg.__all__)


def test_canonical_assignment_package_star_import_matches_all():
    import apps.ridehail.assignment as pkg

    assert _star_imported_names("apps.ridehail.assignment") == set(pkg.__all__)


def test_canonical_analytics_package_star_import_matches_all():
    import apps.ridehail.analytics as pkg

    assert _star_imported_names("apps.ridehail.analytics") == set(pkg.__all__)
