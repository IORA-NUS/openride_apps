# OpenRide — container-logistics simulation.
#
# One image, several roles. The same build runs the Celery agent host, the
# dataplane, the control agent and one-off simulation runs; docker-compose picks
# the role via `command`. See docker-compose.yml.
#
#   docker build -t openride-apps .
#
# What this image deliberately does NOT contain:
#   - the OSRM routing graph (~11 GB) — mounted, see docker-compose.yml
#   - the Next.js dashboard      — separate repo (IORA-NUS/openroad_viz)
#   - the Eve REST API           — separate repo (openride_server/openroad_platform)

FROM python:3.11-slim AS base

# git: orsim is installed from a pinned commit (see requirements.txt).
# graphviz: the `graphviz` Python package needs the binary to render.
# The geo stack (shapely/pyproj/geopandas) ships manylinux wheels, so no
# compiler is needed at install time.
RUN apt-get update && apt-get install -y --no-install-recommends \
        git \
        graphviz \
        curl \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app

WORKDIR /app

# Dependencies first so a source change doesn't re-resolve the whole tree.
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

# ── Runtime image ────────────────────────────────────────────────────────────
FROM base AS runtime

COPY apps/ ./apps/
COPY scenarios/ ./scenarios/
COPY openride_control/ ./openride_control/
COPY openride/ ./openride/

# The real-address catalogue (2.2 MB). This is COPIED, not mounted: the catalog
# is built at module import time, so an image without it cannot even import the
# scenario package. Point OPENRIDE_LOCATIONS_DIR at a mount to override it.
COPY data/ ./data/

VOLUME ["/data/output"]

# Fail fast and loudly on the wrong engine. orsim 1.2.1 — which is what a bare
# `pip install orsim` used to get — imports cleanly but has no `orsim.runtime`,
# so without this the first failure would be a confusing ImportError deep inside
# a run. 1.3.0 released that API, and requirements.txt pins it.
RUN python -c "import orsim.runtime as r; \
    assert hasattr(r, 'AgentSource') and hasattr(r, 'ORSimRuntime'), \
    'orsim.runtime is missing — this is orsim < 1.3.0'; \
    import orsim; print('orsim', orsim.__version__, 'runtime OK')"

# The scenario package builds facility tables at import time, so this proves the
# location data actually landed in the image rather than failing at first run.
RUN python -c "import apps.container_logistics.scenario; \
    from apps.container_logistics.datagen.catalog import default_locations_csv; \
    print('locations:', default_locations_csv())"

# Default role: the Celery agent host. Overridden per service in compose.
CMD ["celery", "-A", "apps.celery_worker", "worker", "--pool", "eventlet", "--concurrency", "64", "--loglevel", "WARNING"]

# ── Dev image: adds the test suite ───────────────────────────────────────────
FROM runtime AS dev
COPY requirements-dev.txt .
RUN pip install -r requirements-dev.txt
COPY tests/ ./tests/
CMD ["pytest", "tests/", "-q", "--continue-on-collection-errors"]
