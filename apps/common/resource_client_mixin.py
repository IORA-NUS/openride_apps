import json
import os
import threading

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from apps.utils import is_success
from apps.config import settings


# ---------------------------------------------------------------------------
# Connection-pooled HTTP session
# ---------------------------------------------------------------------------
# Every previous call did `requests.get(...)` / `requests.patch(...)` at the
# module level — that opens a brand-new TCP connection per call and tears it
# down again afterwards. At 500 trucks × ~2 refreshes/step + state PATCHes,
# the host hits ~8k sockets in TIME_WAIT and burns the bulk of every step on
# connection setup/teardown.
#
# A process-local `requests.Session` with a sized `HTTPAdapter` keeps a pool
# of keep-alive connections per host. Each Celery worker process gets its own
# pool (a Session is not safe to share across forks). Eventlet greenlets
# within a process share the pool fine.
_SESSION_POOL_CONNECTIONS = int(
    os.environ.get("ORSIM_HTTP_POOL_CONNECTIONS", "64")
)
_SESSION_POOL_MAXSIZE = int(
    os.environ.get("ORSIM_HTTP_POOL_MAXSIZE", "256")
)

_session_lock = threading.Lock()
_session_pid: int | None = None
_session: requests.Session | None = None


# KNOWN, UNFIXED: sockets from this pool accumulate in CLOSE-WAIT against the API.
# gunicorn runs `--keep-alive 5` and closes idle connections; the pool holds up to
# `_SESSION_POOL_MAXSIZE` (256) per host indefinitely, so a slot the server already closed sits
# half-open until that slot happens to be reused. Measured 2026-08-16: 865 accumulated across the
# Celery workers, ~1200 after a single 500-truck run, with matching FIN-WAIT-2 server-side.
#
# Harmless so far — 94 fds per worker against a 100000 limit, and urllib3 detects the dropped
# connection on reuse (`is_connection_dropped`) and replaces it without a retry round-trip. It is
# recorded here so the next person does not rediscover it.
#
# DO NOT "fix" this with TCP keepalive: tried 2026-08-16 and it does nothing, because keepalive
# detects a peer that has gone SILENT, whereas a CLOSE-WAIT socket has received an orderly FIN
# and is waiting on a local close() that never comes. Measured CLOSE-WAIT 329 -> 1203 with
# keepalive enabled. A real fix has to either bound connection age/idle time or shrink the pool —
# and shrinking it is what keeps the boot stampede off the connection-setup path, so that is a
# genuine trade, not a free win.
def _build_session() -> requests.Session:
    session = requests.Session()
    # Retry budget sized to survive a multi-second API saturation (e.g. the boot
    # stampede when thousands of agents register at once), not just a single blip.
    # total=5 with backoff_factor=0.3 spans ~0.3+0.6+1.2+2.4+4.8 ~= 9s of retries.
    # POST is included so one-shot creates (facility/truck docs) aren't permanently
    # lost to a transient RemoteDisconnected — Eve creates are effectively idempotent
    # here (run-scoped, name-keyed), so a retried create is safe.
    retry = Retry(
        total=int(os.environ.get("ORSIM_HTTP_RETRY_TOTAL", "5")),
        connect=int(os.environ.get("ORSIM_HTTP_RETRY_TOTAL", "5")),
        read=int(os.environ.get("ORSIM_HTTP_RETRY_TOTAL", "5")),
        backoff_factor=float(os.environ.get("ORSIM_HTTP_RETRY_BACKOFF", "0.3")),
        status_forcelist=(429, 502, 503, 504),
        allowed_methods=frozenset({"GET", "POST", "PATCH", "DELETE", "PUT"}),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(
        pool_connections=_SESSION_POOL_CONNECTIONS,
        pool_maxsize=_SESSION_POOL_MAXSIZE,
        max_retries=retry,
        pool_block=False,
    )
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def _http_session() -> requests.Session:
    """Return a per-process keep-alive session, rebuilding after fork."""
    global _session, _session_pid
    pid = os.getpid()
    if _session is not None and _session_pid == pid:
        return _session
    with _session_lock:
        if _session is None or _session_pid != pid:
            _session = _build_session()
            _session_pid = pid
        return _session


# Public alias so other HTTP clients (state-transition PATCHes, user registry
# login, analytics POSTs, facility lookups, …) can share the same pool. All
# bare ``requests.get/post/patch`` calls in hot paths should be routed
# through this — otherwise pooling on /this/ mixin only is undermined by
# unpooled callers torching connections in parallel.
def get_http_session() -> requests.Session:
    return _http_session()


class ResourceClientMixin:
    """
    Mixin for generic RESTful resource operations and URL construction.
    Expects self.run_id, self.settings, self.simulation_domain, and self.user.

    All HTTP calls go through a shared, connection-pooled session so we don't
    open + close a TCP socket per call (see module-level docs above).
    """

    def _resource_url(self, resource_id=None):
        base = f"{settings['OPENRIDE_SERVER_URL']}/{self.simulation_domain}"
        url = f"{base}/{self.run_id}/{self.persona.get('role')}"
        if resource_id is not None:
            url = f"{url}/{resource_id}"
        return url

    def resource_get(self, resource_id=None, params={}, timeout=None):
        url = self._resource_url(resource_id)
        response = _http_session().get(
            url,
            headers=self.user.get_headers(),
            timeout=timeout or settings.get('NETWORK_REQUEST_TIMEOUT', 10),
            params=params,
        )
        self._check_response(response)
        return response.json()

    def resource_post(self, data, timeout=None):
        url = self._resource_url()
        response = _http_session().post(
            url,
            headers=self.user.get_headers(),
            data=json.dumps(data),
            timeout=timeout or settings.get('NETWORK_REQUEST_TIMEOUT', 10),
        )
        self._check_response(response)
        return response.json()

    def resource_patch(self, resource_id, data, etag=None, timeout=None):
        url = self._resource_url(resource_id)
        headers = self.user.get_headers(etag=etag)
        response = _http_session().patch(
            url,
            headers=headers,
            data=json.dumps(data),
            timeout=timeout or settings.get('NETWORK_REQUEST_TIMEOUT', 10),
        )
        self._check_response(response)
        # Previously this code path issued an extra GET right after every PATCH
        # to "refresh resource after patch", discarded the result, and doubled
        # the request volume of every state transition. Callers that actually
        # need the updated resource call ``manager.refresh()`` explicitly, so
        # the throwaway GET is just connection churn — dropped.
        return response.json()

    def _check_response(self, response):
        if not is_success(response.status_code):
            raise Exception(f"{response.url}, {response.text}")
