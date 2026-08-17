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
    },
}