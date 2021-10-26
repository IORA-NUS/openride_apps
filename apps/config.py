import logging

settings = {
    'OPENRIDE_SERVER_URL': 'http://127.0.0.1:11654',

    'ROUTING_SERVER': 'http://localhost:50001',

    'RABBITMQ_MANAGEMENT_SERVER': "http://localhost:15672/api",
    'RABBITMQ_ADMIN_USER': 'guest',
    'RABBITMQ_ADMIN_PASSWORD': 'guest',

    'MQTT_BROKER': "localhost",

    'WEB_MQTT_PORT': 15675,

    'EXECUTION_STRATEGY': 'CELERY', #  'CELERY'
    'CONCURRENCY_STRATEGY': 'EVENTLET', # 'ASYNCIO', 'EVENTLET'

    # logging
    'LOG_LEVEL': logging.INFO,

    'SIM_SETTINGS': {
        'SIM_DURATION': 120, # 960, # 600,    # 60 # Num Steps
        'SIM_STEP_SIZE': 30, # 15, # 6,     # 60   # seconds
        'NUMSTEPS_BETWEEN_SOLVER': 1, #2,

        'NUM_DRIVERS': 5,       # 100,
        'NUM_PASSENGERS': 30,   # 300,

        'PLANNING_AREA': 'CLEMENTI',

        'PUBLISH_REALTIME_DATA': False,
        'WEBSOCKET_SERVICE': 'MQTT', # 'WS', # 'MQTT'
        'WS_SERVER': 'ws://172.21.177.199:8003', # 'ws://172.27.114.105:3210', # 'ws://localhost:3210', #'ws://172.27.114.105:3210', # Needed only if WEBSOCKET_SERVICE is WS
        'WRITE_WS_OUTPUT_TO_FILE': False,

        'PUBLISH_PATHS_HISTORY': True,
        'WRITE_PH_OUTPUT_TO_FILE': True,
        'PATHS_HISTORY_TIME_WINDOW': 1*30*60, # 900 # seconds

        'STEP_TIMEOUT': 15, # Max Compute time for each step (seconds)
    },

}

# logging.basicConfig(filename='app.log', level=settings['LOG_LEVEL'])


