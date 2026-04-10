import logging

settings = {
    'OPENRIDE_SERVER_URL': 'http://localhost:11654', #'http://192.168.10.135:11654', #'http://127.0.0.1:11654',

    'ROUTING_SERVER': 'http://localhost:10001', # 'http://192.168.10.135:50001', #'http://localhost:50001',


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
    # Add other domains as needed
}

kafka_config = {
    "bootstrap_servers": "localhost:9094",
    "topics": {
        "kpi": "kpi_stream",
        "waypoint": "waypoint_stream",
        "location_stream": "location_stream",
        "route_stream": "route_stream",
    },
    "producer": {
        "linger_ms": 20,
        "batch_size": 262144,
        "compression_type": "zstd",
        "enable_idempotence": True,
        "acks": "all",
        "max_in_flight_requests_per_connection": 5,
    },
    "topic_bootstrap": {
        "kpi": {"partitions": 1, "replication_factor": 1},
        "waypoint": {"partitions": 256, "replication_factor": 1},
        "location_stream": {"partitions": 128, "replication_factor": 1},
        "route_stream": {"partitions": 128, "replication_factor": 1},
    },
}


