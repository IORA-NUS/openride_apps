import os
import logging


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


settings = {
    # Allow override for local development/testing.
    'OPENRIDE_SERVER_URL': os.getenv('OPENRIDE_SERVER_URL', 'http://localhost:11654'),

    'ROUTING_SERVER': os.getenv('ROUTING_SERVER', 'http://localhost:10001'),


    'EXECUTION_STRATEGY': 'CELERY', #  'CELERY'
    'CONCURRENCY_STRATEGY': 'EVENTLET', # 'ASYNCIO', 'EVENTLET'

    # logging
    'LOG_LEVEL': logging.INFO,

    'WEBSOCKET_SERVICE': 'MQTT',  #'WS', # 'MQTT'
    'WS_SERVER': 'ws://192.168.10.135:8003', # 'ws://localhost:8003', # 'ws://172.27.114.105:3210', # 'ws://localhost:3210', #'ws://172.27.114.105:3210', # Needed only if WEBSOCKET_SERVICE is WS

    'NETWORK_REQUEST_TIMEOUT': 10,      # Seconds

    # KPI pipeline: stream-only during run; Mongo filled at end by kpi-duckdb-sink.
    'KPI_WRITE_MONGO_DURING_RUN': _env_bool('KPI_WRITE_MONGO_DURING_RUN', False),

}

messenger_backend = {
    'RABBITMQ_MANAGEMENT_SERVER': "http://localhost:15672/api", # "http://192.168.10.135:15672/api", # "http://localhost:15672/api",
    'RABBITMQ_ADMIN_USER': 'guest', # 'test', # 'guest',
    'RABBITMQ_ADMIN_PASSWORD': 'guest', #'test', # 'guest',

    'MQTT_BROKER': "localhost", # "192.168.10.115", # "localhost",
    'WEB_MQTT_PORT': 15675,
}

# Simulation domains mapping (URL prefix for OpenRide API resources).
simulation_domains = {
    'ridehail': 'ridehail-sim',
    'container_logistics': 'container-logistics-sim',
}

# Logical KPI catalog ecosystem names (used by GET /kpis?ecosystem=...).
# These differ from simulation_domains: e.g. container-logistics-sim vs container_logistics.
kpi_ecosystems = {
    'ridehail': 'ridehail',
    'container_logistics': 'container_logistics',
}


# KPI DuckDB sink (see apps/kpi_sink/).
kpi_sink_settings = {
    "data_dir": os.getenv("KPI_DUCKDB_DATA_DIR", os.path.join(os.path.expanduser("~"), ".openride", "kpi-duckdb")),
    "consumer_group": os.getenv("KPI_DUCKDB_SINK_GROUP", "kpi-duckdb-sink"),
    "batch_ms": int(os.getenv("KPI_DUCKDB_BATCH_MS", "500")),
    "batch_max_rows": int(os.getenv("KPI_DUCKDB_BATCH_MAX_ROWS", "500")),
    "from_beginning": _env_bool("KPI_DUCKDB_FROM_BEGINNING", False),
    # Phase 2: per-entity breakdown → DuckDB (consumed from kpi_breakdown_stream) + an in-process
    # read API the dashboard queries (BREAKDOWN_SOURCE=duck). Replay from start so a sink launched
    # mid-run doesn't miss a run's earlier breakdown snapshots (rows upsert idempotently).
    "breakdown_group": os.getenv("KPI_DUCKDB_BREAKDOWN_GROUP", "kpi-duckdb-breakdown"),
    "breakdown_from_beginning": _env_bool("KPI_DUCKDB_BREAKDOWN_FROM_BEGINNING", True),
    "http_host": os.getenv("KPI_DUCKDB_HTTP_HOST", "127.0.0.1"),
    "http_port": int(os.getenv("KPI_DUCKDB_HTTP_PORT", "8615")),
    "http_enabled": _env_bool("KPI_DUCKDB_HTTP_ENABLED", True),
    "export_to_mongo_on_complete": _env_bool("KPI_EXPORT_TO_MONGO_ON_COMPLETE", True),
    "export_batch_size": int(os.getenv("KPI_EXPORT_BATCH_SIZE", "1000")),
    "force_reexport": _env_bool("KPI_FORCE_REEXPORT", False),
    "mongo_host": os.getenv("MONGODB_HOST", "localhost"),
    "mongo_port": int(os.getenv("MONGODB_PORT", "27017")),
    "mongo_db": os.getenv("MONGODB_NAME", "OpenRoadDB"),
    "mongo_uri": os.getenv("MONGODB_URI"),
}


# Fast local path: idempotence needs the transaction coordinator (slow right after broker restart).
_KAFKA_IDEMPOTENT = _env_bool("KAFKA_ENABLE_IDEMPOTENCE", False)

kafka_config = {
    "bootstrap_servers": os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9094"),
    "topics": {
        "run_status": "run_status",
        "kpi": "kpi_stream",
        "kpi_breakdown": "kpi_breakdown_stream",
        "trip_geo": "trip_geo_stream",
        "facility_stream": "facility_stream",
        "perf": "perf_stream",
        "service_control": "service_control",
        "service_status": "service_status",
        "service_events": "service_events",
        # Phase-2 transport: the cross-process channel that carries each celery
        # worker's ORSim transport counters to the process that writes the run's
        # outcome. Without it those counters exist only in
        # `journalctl --user -u openride-celery`, in ~64 separate processes, and
        # a run that lost agent replies reports `ok: true` -- the CLAUDE.md §8 /
        # §6.23 shape. See apps/transport/health_stream.py.
        "transport_health": "transport_health_stream",
    },
    "producer": {
        "linger_ms": 5,
        "batch_size": 262144,
        "compression_type": "lz4" if _KAFKA_IDEMPOTENT else "none",
        "enable_idempotence": _KAFKA_IDEMPOTENT,
        "acks": "all" if _KAFKA_IDEMPOTENT else "1",
        "max_in_flight_requests_per_connection": 5 if _KAFKA_IDEMPOTENT else 1,
    },
    # Dedicated low-latency producer for trip geometry (small batches, no linger).
    "producer_trip_geo": {
        "linger_ms": 0,
        "batch_size": 65536,
        "compression_type": "lz4" if _KAFKA_IDEMPOTENT else "none",
        "enable_idempotence": _KAFKA_IDEMPOTENT,
        "acks": "all" if _KAFKA_IDEMPOTENT else "1",
        "max_in_flight_requests_per_connection": 5 if _KAFKA_IDEMPOTENT else 1,
    },
    "producer_facility_stream": {
        "linger_ms": 0,
        "batch_size": 65536,
        "compression_type": "lz4" if _KAFKA_IDEMPOTENT else "none",
        "enable_idempotence": _KAFKA_IDEMPOTENT,
        "acks": "all" if _KAFKA_IDEMPOTENT else "1",
        "max_in_flight_requests_per_connection": 5 if _KAFKA_IDEMPOTENT else 1,
    },
    # Low-latency producer for perf_stream (realtime dashboard).
    "producer_perf": {
        "linger_ms": 0,
        "batch_size": 65536,
        "compression_type": "lz4" if _KAFKA_IDEMPOTENT else "none",
        "enable_idempotence": _KAFKA_IDEMPOTENT,
        "acks": "all" if _KAFKA_IDEMPOTENT else "1",
        "max_in_flight_requests_per_connection": 5 if _KAFKA_IDEMPOTENT else 1,
    },
    "topic_bootstrap": {
        "run_status": {"partitions": 1, "replication_factor": 1, "config": {"cleanup.policy": "compact"}},
        "kpi": {"partitions": 1, "replication_factor": 1},
        "kpi_breakdown": {"partitions": 1, "replication_factor": 1},
        "trip_geo": {"partitions": 1, "replication_factor": 1},
        "facility_stream": {"partitions": 1, "replication_factor": 1},
        "perf": {"partitions": 1, "replication_factor": 1},
        "service_control": {"partitions": 1, "replication_factor": 1},
        "service_status": {
            "partitions": 1,
            "replication_factor": 1,
            "config": {"cleanup.policy": "compact"},
        },
        "service_events": {
            "partitions": 1,
            "replication_factor": 1,
            "config": {"retention.ms": "604800000"},
        },
        # 24 h is plenty: it is read at the end of the run that wrote it.
        "transport_health": {
            "partitions": 1,
            "replication_factor": 1,
            "config": {"retention.ms": "86400000"},
        },
    },
}

# ---------------------------------------------------------------------------
# Agent transport selection (RabbitMQ -> Kafka migration, phase 2).
#
# `paho` is today's behaviour and the default: `build_agent_transport` returns
# None for it, so `Messenger` builds a `PahoTransport` on exactly today's code
# path.  Nothing below is read at all until someone sets the flag to `kafka`.
#
# THE FLAG IS STAMPED ONTO `messenger_backend` ON PURPOSE.  `messenger_backend`
# is what `ORSimEnv.set_backend()` receives inside the celery worker, and it is
# the ONLY channel that crosses the JSON-serialised celery task boundary.
# Workers here are long-lived across runs, so a worker that re-read
# OPENRIDE_AGENT_TRANSPORT from its own environment could serve run A on paho
# and run B on kafka, or half-migrate a single run if the variable changed
# mid-flight.  Stamping it here means the LAUNCHING process decides once and the
# choice rides the task payload.  See apps/transport/factory.py.
#
# KAFKA_APP_PARTITIONS is the knob for the one number this design does not yet
# have a measurement for: app-lane fan-out amplification.  A process assigns only
# the partitions its ~17 resident agents hash into (~15 of 64), so lowering it
# raises the redundant-decode count linearly.  Do NOT set it to 1 "for
# simplicity" -- that is a 64x amplification of ~200k records per run.
# ---------------------------------------------------------------------------
# The trailing `or "paho"` matters: an env var exported as EMPTY
# (`OPENRIDE_AGENT_TRANSPORT=` in a systemd unit or a shell) would otherwise
# stamp the empty string, and a downstream reader that treats "" as "unknown"
# instead of "unset" would refuse to start the run.
agent_transport = os.getenv("OPENRIDE_AGENT_TRANSPORT", "paho").strip().lower() or "paho"

messenger_backend["AGENT_TRANSPORT"] = agent_transport
messenger_backend["KAFKA_BOOTSTRAP_SERVERS"] = kafka_config["bootstrap_servers"]
messenger_backend["KAFKA_APP_PARTITIONS"] = int(os.getenv("KAFKA_APP_PARTITIONS", "64"))
messenger_backend["KAFKA_REPLY_PARTITIONS"] = int(os.getenv("KAFKA_REPLY_PARTITIONS", "8"))
# KAFKA_LINGER_MS defaults to 0 because the barrier's critical path is two
# single-record publishes (the scheduler's broadcast, the agent's reply) with
# nothing to batch against: measured n=3 per arm, linger=5 cost ~10 ms per step
# -- ~25 s on a 2592-step run -- and bought nothing. Ordering rests on
# max.in.flight=1, not on linger. See _producer_config in kafka_bus.py.
messenger_backend["KAFKA_LINGER_MS"] = int(os.getenv("KAFKA_LINGER_MS", "0"))
# The producer's delivery budget. librdkafka's own default is 300_000 ms, which
# leaves the ONLY error channel this design has blind for five minutes -- longer
# than STEP_TIMEOUT, longer than ORSim's 30 s heartbeat threshold, and longer
# than BUS_LINGER_S, so a total publish outage moved no counter and wrote no log
# line until long after the affected agents had been pruned.
messenger_backend["KAFKA_MESSAGE_TIMEOUT_MS"] = int(
    os.getenv("KAFKA_MESSAGE_TIMEOUT_MS", "10000")
)
# 0 on purpose: retries>0 without idempotence permits broker-side DUPLICATES,
# and nothing in ORSim dedupes on time_step -- a duplicated step broadcast makes
# every agent step twice for one time step, which shifts KPIs with no counter
# moving. A LOST record is counted in delivery_failures and makes the run
# non-quotable; visible beats silent.
messenger_backend["KAFKA_PRODUCER_RETRIES"] = int(
    os.getenv("KAFKA_PRODUCER_RETRIES", "0")
)
