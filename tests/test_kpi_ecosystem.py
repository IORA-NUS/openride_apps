from apps.config import kpi_ecosystems, simulation_domains
from apps.utils.kpi_ecosystem import normalize_kpi_ecosystem


def test_normalize_kpi_ecosystem_accepts_logical_name():
    assert normalize_kpi_ecosystem(kpi_ecosystems["container_logistics"]) == "container_logistics"
    assert normalize_kpi_ecosystem(kpi_ecosystems["ridehail"]) == "ridehail"


def test_normalize_kpi_ecosystem_maps_simulation_domain():
    assert (
        normalize_kpi_ecosystem(simulation_domains["container_logistics"])
        == "container_logistics"
    )
    assert normalize_kpi_ecosystem(simulation_domains["ridehail"]) == "ridehail"


def test_normalize_kpi_ecosystem_passes_through_unknown():
    assert normalize_kpi_ecosystem("custom_ecosystem") == "custom_ecosystem"
