import pytest

import apps.ride_hail.analytics as analytics_pkg
import apps.ride_hail.assignment as assignment_pkg
import apps.ride_hail.driver as driver_pkg
import apps.ride_hail.passenger as passenger_pkg


def test_driver_package_all_symbols_resolve():
    for name in driver_pkg.__all__:
        assert getattr(driver_pkg, name) is not None


def test_passenger_package_all_symbols_resolve():
    for name in passenger_pkg.__all__:
        assert getattr(passenger_pkg, name) is not None


def test_assignment_package_all_symbols_resolve():
    for name in assignment_pkg.__all__:
        assert getattr(assignment_pkg, name) is not None


def test_analytics_package_all_symbols_resolve():
    for name in analytics_pkg.__all__:
        assert getattr(analytics_pkg, name) is not None


def test_canonical_packages_unknown_attribute_raises_attribute_error():
    for pkg in [driver_pkg, passenger_pkg, assignment_pkg, analytics_pkg]:
        with pytest.raises(AttributeError):
            getattr(pkg, "DoesNotExist")
