import pytest

import apps.assignment_app as assignment_pkg
import apps.driver_app as driver_pkg
import apps.passenger_app as passenger_pkg


def test_driver_package_getattr_known_symbols_resolve():
    for name in driver_pkg.__all__:
        assert getattr(driver_pkg, name) is not None


def test_passenger_package_getattr_known_symbols_resolve():
    for name in passenger_pkg.__all__:
        assert getattr(passenger_pkg, name) is not None


def test_assignment_package_getattr_known_symbols_resolve():
    for name in assignment_pkg.__all__:
        assert getattr(assignment_pkg, name) is not None


def test_driver_package_getattr_unknown_symbol_raises_attribute_error():
    with pytest.raises(AttributeError):
        getattr(driver_pkg, "DoesNotExist")


def test_passenger_package_getattr_unknown_symbol_raises_attribute_error():
    with pytest.raises(AttributeError):
        getattr(passenger_pkg, "DoesNotExist")


def test_assignment_package_getattr_unknown_symbol_raises_attribute_error():
    with pytest.raises(AttributeError):
        getattr(assignment_pkg, "DoesNotExist")
