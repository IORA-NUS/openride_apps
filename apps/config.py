import os
import logging

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

}

messenger_backend = {
    'RABBITMQ_MANAGEMENT_SERVER': "http://localhost:15672/api", # "http://192.168.10.135:15672/api", # "http://localhost:15672/api",
    'RABBITMQ_ADMIN_USER': 'guest', # 'test', # 'guest',
    'RABBITMQ_ADMIN_PASSWORD': 'guest', #'test', # 'guest',

    'MQTT_BROKER': "localhost", # "192.168.10.115", # "localhost",
    'WEB_MQTT_PORT': 15675,
}

# Simulation domains mapping
simulation_domains = {
    'ridehail': 'ridehail-sim',
    'container_logistics': 'container-logistics-sim',
}


kafka_config = {
    "bootstrap_servers": "localhost:9094",
    "topics": {
        "run_status": "run_status",
        "kpi": "kpi_stream",
        "trip_geo": "trip_geo_stream",
    },
    "producer": {
        "linger_ms": 20,
        "batch_size": 262144,
        "compression_type": "zstd",
        "enable_idempotence": True,
        "acks": "all",
        "max_in_flight_requests_per_connection": 5,
    },
    # Dedicated low-latency producer for trip geometry (small batches, no linger).
    "producer_trip_geo": {
        "linger_ms": 0,
        "batch_size": 65536,
        "compression_type": "zstd",
        "enable_idempotence": True,
        "acks": "all",
        "max_in_flight_requests_per_connection": 5,
    },
    "topic_bootstrap": {
        "run_status": {"partitions": 1, "replication_factor": 1, "config": {"cleanup.policy": "compact"}},
        "kpi": {"partitions": 1, "replication_factor": 1},
        "trip_geo": {"partitions": 1, "replication_factor": 1},
    },
}