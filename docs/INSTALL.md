# Installing OpenRide

A complete build of the simulation engine, the platform API, the streaming data
plane and the analytics dashboard — from an empty machine to a finished
500-truck run.

Work through the phases in order; each depends on the one before it.

| | |
|---|---|
| Time to first run | 60–90 min |
| Disk required | ~25 GB |
| Repositories | 3 + 1 engine |
| Platform | Linux / macOS |

> A typeset version of this guide, with the same content, lives at
> `docs/install-guide.html`. Open it in a browser or print it to PDF.

---

## 00 · Prerequisites

| Requirement | Version | Used for |
|---|---|---|
| Python | 3.11 | Simulation, agents, control plane, CLI |
| Node.js | 20 or newer | Analytics dashboard and stream sinks |
| Docker | 24 or newer | Databases, broker, API, routing engine |
| Docker Compose | v2 (built in) | Service orchestration |
| Git | 2.30 or newer | Submodule support |
| systemd | user session | Background workers (Linux only) |

**Repository access.** You need an SSH key registered with GitHub that has read
access to the `IORA-NUS` organisation. The dashboard is a private repository
pulled in as a submodule, so HTTPS cloning without credentials will not work.

On macOS, skip the systemd phase and run the background workers manually — the
commands are given in that section.

---

## 01 · Get the code

Everything lives under one workspace directory. The platform derives its own
paths from this layout, so keep the structure as shown — the folder names
matter, the location does not.

```
openride/                     ← your workspace root
├── openride_apps/            simulation, agents, CLI, control plane
├── openride_server/          platform API, infrastructure
│   └── analytics/            dashboard (git submodule)
└── kafka_broker/             message broker (created in phase 03)
```

```bash
# Choose a workspace root and remember it — later phases reference it.
export OPENRIDE_HOME="$HOME/openride"
mkdir -p "$OPENRIDE_HOME" && cd "$OPENRIDE_HOME"

# Simulation engine and agents
git clone git@github.com:IORA-NUS/openride_apps.git

# Platform API + dashboard submodule — --recurse-submodules is required
git clone --recurse-submodules git@github.com:IORA-NUS/openride_server.git
```

Confirm the submodule came down with it — the dashboard directory should
contain a `package.json`, not be empty:

```bash
ls "$OPENRIDE_HOME/openride_server/analytics/package.json"
```

If it is empty, run `git submodule update --init` inside `openride_server`.
That happens when a repository is cloned without `--recurse-submodules`.

---

## 02 · Python environment

One virtual environment serves the simulation, the agents, the control plane
and the command-line interface. The simulation engine installs from a published
tag, so no local checkout of it is needed.

```bash
cd "$OPENRIDE_HOME/openride_apps"

python3.11 -m venv venv
./venv/bin/pip install --upgrade pip setuptools wheel
./venv/bin/pip install -r requirements.txt

# Optional — adds pytest and the test tooling
./venv/bin/pip install -r requirements-dev.txt
```

Verify the engine resolved. Both must print, and the version must read `1.3.0`
or later:

```bash
./venv/bin/python -c "import orsim; print(orsim.__version__)"
./venv/bin/python -c "import orsim.runtime; print('runtime ok')"
```

The geospatial stack (`shapely`, `pyproj`, `geopandas`) installs from pre-built
wheels, so no compiler is normally required. If a package does try to build from
source, install your distribution's `python3-dev` and `build-essential` and
retry.

---

## 03 · Message broker

Kafka carries telemetry between the simulation and every consumer — metrics, run
status, truck positions and route geometry. It runs in its own Compose project
and **creates the shared Docker network the platform services attach to, so
bring it up first.**

```bash
mkdir -p "$OPENRIDE_HOME/kafka_broker"
cat > "$OPENRIDE_HOME/kafka_broker/docker-compose.yml" <<'YAML'
services:
  kafka:
    image: apache/kafka:latest
    container_name: kafka
    restart: unless-stopped
    ports: ["9092:9092", "9094:9094"]
    environment:
      KAFKA_NODE_ID: 1
      KAFKA_PROCESS_ROLES: broker,controller
      KAFKA_LISTENERS: PLAINTEXT://:9092,CONTROLLER://:9093,EXTERNAL://:9094
      KAFKA_ADVERTISED_LISTENERS: PLAINTEXT://kafka:9092,EXTERNAL://localhost:9094
      KAFKA_LISTENER_SECURITY_PROTOCOL_MAP: PLAINTEXT:PLAINTEXT,CONTROLLER:PLAINTEXT,EXTERNAL:PLAINTEXT
      KAFKA_CONTROLLER_LISTENER_NAMES: CONTROLLER
      KAFKA_CONTROLLER_QUORUM_VOTERS: 1@kafka:9093
      KAFKA_INTER_BROKER_LISTENER_NAME: PLAINTEXT
      KAFKA_OFFSETS_TOPIC_REPLICATION_FACTOR: 1
      KAFKA_AUTO_CREATE_TOPICS_ENABLE: "true"
      CLUSTER_ID: "q1Sh-9_ISia_zwGINzRvyQ"
    volumes: [kafka_data:/var/lib/kafka/data]
    networks: [kafka-net]

  kafka-ui:
    image: provectuslabs/kafka-ui
    container_name: kafka-ui
    ports: ["8080:8080"]
    environment:
      KAFKA_CLUSTERS_0_NAME: local
      KAFKA_CLUSTERS_0_BOOTSTRAPSERVERS: kafka:9092
    depends_on: [kafka]
    networks: [kafka-net]

volumes:
  kafka_data:

networks:
  kafka-net:
    name: kafka-server-bridge
YAML

cd "$OPENRIDE_HOME/kafka_broker" && docker compose up -d
```

Check the shared network exists. The platform stack expects it by this exact
name and will refuse to start without it:

```bash
docker network ls | grep kafka-server-bridge
```

---

## 04 · Platform services

Brings up the database, the REST API behind its gateway, the metrics store and
the stream sinks. The API image builds from source on first run.

```bash
cd "$OPENRIDE_HOME/openride_server"
docker compose up -d --build mongodb api nginx victoriametrics perf-vm-sink kpi-vm-sink
```

The task queue that hosts the simulation agents lives in the other repository:

```bash
cd "$OPENRIDE_HOME/openride_apps"
docker compose up -d rabbit
```

Confirm the API is answering. **A `401` is the expected, healthy response** — it
means the resource is registered and asking for credentials:

```bash
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:11654/
```

---

## 05 · Routing engine

Trucks follow real roads. OSRM serves those routes from a pre-processed extract
of the Malaysia–Singapore–Brunei road network, built once from an OpenStreetMap
download.

> **Plan for this step.** The download is roughly 240 MB and the processed graph
> expands to about 11 GB. Pre-processing takes 10–30 minutes. Nothing else in the
> setup depends on it, so you can start it and continue with phase 06 in another
> terminal.

```bash
mkdir -p "$OPENRIDE_HOME/openride_server/maps"
cd "$OPENRIDE_HOME/openride_server/maps"

curl -L -o malaysia-singapore-brunei-260305.osm.pbf \
  https://download.geofabrik.de/asia/malaysia-singapore-brunei-latest.osm.pbf
```

Keep that filename — the Compose service references it directly.

```bash
cd "$OPENRIDE_HOME/openride_server"

docker compose --profile osrm-preprocess run --rm osrm-extract
docker compose --profile osrm-preprocess run --rm osrm-partition
docker compose --profile osrm-preprocess run --rm osrm-customize

docker compose up -d osrm-routed
```

Test a route between two points in Singapore. A healthy response contains
`"code":"Ok"`:

```bash
curl -s "http://localhost:10001/route/v1/driving/103.85,1.29;103.75,1.35" | head -c 120
```

---

## 06 · Dashboard

```bash
cd "$OPENRIDE_HOME/openride_server/analytics"

npm ci
cp .env.example .env.local

npm run build
```

The defaults in `.env.example` point at the services from phases 03–05 and need
no editing for a single-machine install.

> **Build before type-checking.** Run `npm run build` at least once before
> `npx tsc --noEmit`. The build generates the type declarations for image
> imports; running the type-checker first on a fresh clone reports
> missing-module errors that resolve themselves afterwards.

---

## 07 · Background workers

Five long-running processes complete the stack: the Celery agent pool, the data
plane, the route-geometry sink, the control agent and the dashboard server. On
Linux a single script installs and enables them as systemd user services.

```bash
cd "$OPENRIDE_HOME/openride_apps"
export OPENRIDE_WORKSPACE_ROOT="$OPENRIDE_HOME"

bash scripts/start_openride.sh
```

The script installs the unit files from `systemd/`, points them at your
workspace, enables them at login and brings them up.

```bash
systemctl --user list-units 'openride-*' --no-pager
```

To keep them running when you are not logged in:

```bash
loginctl enable-linger "$USER"
```

### Running without systemd

On macOS, or to manage the processes yourself, run each in its own terminal from
`openride_apps`:

```bash
bash scripts/run-celery.sh          # simulation agent pool
bash scripts/run-dataplane.sh       # stream ingest + read API
bash scripts/run-analytics-dev.sh   # dashboard on :3000
```

---

## 08 · Compile scenarios

A scenario is defined by a `spec.json` that ships with the repository. The
runnable bundle — the fleet, the orders, the facilities and the road network
between them — is generated from that specification on your machine.

```bash
cd "$OPENRIDE_HOME/openride_apps"

./venv/bin/python -m openride.cli scenario list
./venv/bin/python -m openride.cli scenario compile multi_haulier_greedy_solver_500_trucks -y
```

To build every shipped scenario — around 90 seconds in total:

```bash
for d in scenarios/*/; do
  s=$(basename "$d")
  [ -f "$d/spec.json" ] && ./venv/bin/python -m openride.cli scenario compile "$s" -y
done
```

Bundles are generated artefacts and are deliberately not stored in the
repository — the specification is the source of truth. Re-run this after pulling
changes that touch a scenario.

---

## 09 · Verify the installation

Each check isolates a different layer, so a failure tells you which phase to
revisit.

| Check | How |
|---|---|
| Every service reports running | `./venv/bin/python -m openride_control.command status` |
| Dashboard loads | <http://127.0.0.1:3000> |
| Platform API answers with 401 | `curl -s -o /dev/null -w "%{http_code}" http://localhost:11654/` |
| Data plane answers with 200 | `curl -s -o /dev/null -w "%{http_code}" http://localhost:8620/runs/live` |
| Routing engine returns a route | `curl -s "http://localhost:10001/route/v1/driving/103.85,1.29;103.75,1.35"` |
| Kafka broker reachable | <http://localhost:8080> |
| Test suite runs | `./venv/bin/python -m pytest -q --continue-on-collection-errors` |

The container-logistics suite passes in full. A number of failures and
collection errors in the legacy ride-hail modules are expected on a clean
install and do not affect the simulation.

---

## 10 · Your first run

A seven-day, 500-truck simulation across three haulage companies. Five to six
minutes, printing progress as it advances through simulated days.

```bash
cd "$OPENRIDE_HOME/openride_apps"

bash scripts/openride.sh run multi_haulier_greedy_solver_500_trucks \
  --solver GreedyNearest --json
```

On completion you get a JSON summary with the run identifier, wall-clock
duration, completed orders and the headline fleet metrics. Open the dashboard
and select that run identifier to explore the map, the per-company breakdown and
the comparison views.

To watch trucks move on the live map while the run is in progress, add
`--no-headless` — this enables per-step position streaming, at some cost to run
time.

With no arguments the CLI opens an interactive menu:

```bash
bash scripts/openride.sh
```

---

## Reference

### Service ports

| Port | Service | Notes |
|---|---|---|
| 3000 | Analytics dashboard | The main interface |
| 8080 | Kafka UI | Topic and message inspection |
| 8428 | VictoriaMetrics | Performance metrics store |
| 8620 | Data plane | Read API for runs and metrics |
| 9092 | Kafka | Internal listener |
| 9094 | Kafka | External listener, used by the dashboard |
| 10001 | OSRM | Road routing |
| 11654 | Platform API | Behind the nginx gateway |
| 15672 | RabbitMQ | Management console |
| 27017 | MongoDB | Primary database |

### Environment variables

All optional — the platform derives sensible values from its own location. Set
them when your layout differs from the default.

| Variable | Purpose |
|---|---|
| `OPENRIDE_WORKSPACE_ROOT` | Directory containing `openride_apps` and `openride_server` |
| `OPENRIDE_PYTHON` | Interpreter used to run the simulation |
| `OPENRIDE_LOCATIONS_DIR` | Alternative location for the address and region data |
| `MONGODB_URI` | Database connection string |
| `KAFKA_BROKER_URL` | Broker address for the dashboard |
| `OSRM_MAPS_DIR` | Alternative location for the routing graph |

### Everyday commands

| Command | Does |
|---|---|
| `scripts/openride.sh` | Interactive menu |
| `scripts/openride.sh run <scenario>` | Launch a simulation |
| `scripts/openride.sh scenario list` | Show available scenarios |
| `scripts/openride.sh runs list` | Show past runs |
| `scripts/start_openride.sh` | Install and start background services |
| `scripts/stop_openride.sh` | Stop background services |

### Restarting after changes

- Editing agent code — truck, order, facility, assignment or analytics —
  requires `systemctl --user restart openride-celery`. The agents run inside
  long-lived workers that hold the previously imported code.
- Editing API models or adding a REST resource requires rebuilding the API
  container: `docker compose up -d --build api`.
- Editing a scenario specification requires re-running the compile step in
  phase 08.
