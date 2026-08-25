import logging
import time

import requests

from apps.common.resource_client_mixin import get_http_session
from apps.config import settings
from apps.utils.kpi_ecosystem import normalize_kpi_ecosystem


class KpiCatalogClient:
    """
    Small client to fetch and cache KPI definitions from OpenRide server.

    Used by simulations to enforce "only emit KPIs that exist in the catalog".
    """

    def __init__(self, ecosystem: str, user, ttl_seconds: int | None = None):
        self.ecosystem = normalize_kpi_ecosystem(ecosystem)
        self.user = user
        self.ttl_seconds = ttl_seconds if ttl_seconds is not None else settings.get("KPI_CATALOG_CACHE_TTL", 60)
        self._cached_keys: set[str] | None = None
        self._cached_at: float = 0.0
        self._catalog_missing: bool = False

    def _should_refresh(self) -> bool:
        if self._cached_keys is None:
            return True
        return (time.time() - self._cached_at) > float(self.ttl_seconds)

    def refresh(self) -> set[str]:
        url = f"{settings['OPENRIDE_SERVER_URL']}/kpis"
        resp = get_http_session().get(
            url,
            headers=self.user.get_headers(),
            params={"ecosystem": self.ecosystem},
            timeout=settings.get("NETWORK_REQUEST_TIMEOUT", 10),
        )
        if resp.status_code == 404:
            logging.warning(
                "KPI catalog returned 404 for ecosystem=%s — no catalog defined. "
                "KPI enforcement will be skipped. Create KPI definitions to enable enforcement.",
                self.ecosystem,
            )
            self._catalog_missing = True
            self._cached_keys = set()
            self._cached_at = time.time()
            return self._cached_keys
        resp.raise_for_status()
        items = resp.json() or []
        keys = {i.get("key") for i in items if i.get("key")}
        if not keys:
            logging.warning(
                "KPI catalog for ecosystem=%s returned no keys. "
                "Ensure kpi_definitions are seeded on the OpenRide server.",
                self.ecosystem,
            )
        else:
            logging.debug(
                "KPI catalog refreshed for ecosystem=%s (%d keys)",
                self.ecosystem,
                len(keys),
            )
        self._cached_keys = keys
        self._cached_at = time.time()
        self._catalog_missing = False
        return keys

    def allowed_keys(self) -> set[str]:
        if self._should_refresh():
            try:
                return self.refresh()
            except Exception:
                logging.exception("Failed to refresh KPI catalog for ecosystem=%s", self.ecosystem)
                # Fail closed for unexpected errors; 404 is handled in refresh() without raising.
                raise
        return self._cached_keys or set()

    def is_allowed(self, metric_key: str) -> bool:
        keys = self.allowed_keys()
        if self._catalog_missing:
            return True
        return metric_key in keys

