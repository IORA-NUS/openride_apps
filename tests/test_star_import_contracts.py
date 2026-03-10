def _star_imported_names(module_name):
    namespace = {}
    exec(f"from {module_name} import *", {}, namespace)
    return {name for name in namespace if not name.startswith("__")}


def test_ride_hail_star_import_matches_root_all():
    import apps.ride_hail as pkg

    assert _star_imported_names("apps.ride_hail") == set(pkg.__all__)


def test_ride_hail_adapters_star_import_matches_all():
    import apps.ride_hail.adapters as pkg

    assert _star_imported_names("apps.ride_hail.adapters") == set(pkg.__all__)


def test_legacy_driver_package_star_import_matches_all():
    import apps.driver_app as pkg

    assert _star_imported_names("apps.driver_app") == set(pkg.__all__)


def test_legacy_passenger_package_star_import_matches_all():
    import apps.passenger_app as pkg

    assert _star_imported_names("apps.passenger_app") == set(pkg.__all__)


def test_legacy_assignment_package_star_import_matches_all():
    import apps.assignment_app as pkg

    assert _star_imported_names("apps.assignment_app") == set(pkg.__all__)


def test_canonical_driver_package_star_import_matches_all():
    import apps.ride_hail.driver as pkg

    assert _star_imported_names("apps.ride_hail.driver") == set(pkg.__all__)


def test_canonical_passenger_package_star_import_matches_all():
    import apps.ride_hail.passenger as pkg

    assert _star_imported_names("apps.ride_hail.passenger") == set(pkg.__all__)


def test_canonical_assignment_package_star_import_matches_all():
    import apps.ride_hail.assignment as pkg

    assert _star_imported_names("apps.ride_hail.assignment") == set(pkg.__all__)


def test_canonical_analytics_package_star_import_matches_all():
    import apps.ride_hail.analytics as pkg

    assert _star_imported_names("apps.ride_hail.analytics") == set(pkg.__all__)
