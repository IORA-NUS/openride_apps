import pytest

import apps.ridehail.adapters as adapters_pkg


def test_ride_hail_adapters_package_all_symbols_resolve():
    for name in adapters_pkg.__all__:
        assert getattr(adapters_pkg, name) is not None


def test_ride_hail_adapters_package_unknown_attribute_raises_attribute_error():
    with pytest.raises(AttributeError):
        getattr(adapters_pkg, "DoesNotExist")
