import logging

settings = {
    'OPENRIDE_SERVER_URL': 'http://localhost:11654', #'http://192.168.10.135:11654', #'http://127.0.0.1:11654',

    'ROUTING_SERVER': 'http://localhost:50001', # 'http://192.168.10.135:50001', #'http://localhost:50001',


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


