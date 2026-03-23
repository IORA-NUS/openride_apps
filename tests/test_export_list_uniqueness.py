import apps.ride_hail as ride_hail
import apps.ride_hail.adapters as ride_hail_adapters
import apps.ride_hail.analytics as canonical_analytics_pkg
import apps.ride_hail.assignment as canonical_assignment_pkg
import apps.ride_hail.driver as canonical_driver_pkg
import apps.ride_hail.passenger as canonical_passenger_pkg


def _assert_all_entries_are_unique(module_obj):
    values = list(module_obj.__all__)
    assert len(values) == len(set(values))


def test_root_and_adapter_export_lists_have_unique_symbols():
    _assert_all_entries_are_unique(ride_hail)
    _assert_all_entries_are_unique(ride_hail_adapters)


def test_canonical_role_package_export_lists_have_unique_symbols():
    _assert_all_entries_are_unique(canonical_driver_pkg)
    _assert_all_entries_are_unique(canonical_passenger_pkg)
    _assert_all_entries_are_unique(canonical_assignment_pkg)
    _assert_all_entries_are_unique(canonical_analytics_pkg)


