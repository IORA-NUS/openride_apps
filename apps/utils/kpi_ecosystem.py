"""Resolve KPI catalog ecosystem identifiers (distinct from simulation URL domains)."""

from apps.config import kpi_ecosystems, simulation_domains


def normalize_kpi_ecosystem(ecosystem: str) -> str:
    """
    Return the logical KPI catalog ecosystem name.

    Accepts either the catalog name (``container_logistics``) or the simulation
    URL domain (``container-logistics-sim``) so callers cannot accidentally query
    an empty catalog.
    """
    if ecosystem in kpi_ecosystems.values():
        return ecosystem
    for sim_key, sim_domain in simulation_domains.items():
        if ecosystem == sim_domain:
            return kpi_ecosystems[sim_key]
    return ecosystem
