import logging

settings = {
    'OPENRIDE_SERVER_URL': 'http://192.168.10.135:11654', #'http://127.0.0.1:11654',

    'ROUTING_SERVER': 'http://localhost:50001', # 'http://192.168.10.135:50001', #'http://localhost:50001',

    'RABBITMQ_MANAGEMENT_SERVER': "http://localhost:15672/api", # "http://192.168.10.135:15672/api", # "http://localhost:15672/api",
    'RABBITMQ_ADMIN_USER': 'guest', # 'test', # 'guest',
    'RABBITMQ_ADMIN_PASSWORD': 'guest', #'test', # 'guest',

    'MQTT_BROKER': "localhost", # "192.168.10.115", # "localhost",
    'WEB_MQTT_PORT': 15675,

    'EXECUTION_STRATEGY': 'CELERY', #  'CELERY'
    'CONCURRENCY_STRATEGY': 'EVENTLET', # 'ASYNCIO', 'EVENTLET'

    # logging
    'LOG_LEVEL': logging.INFO,

    # 'WEBSOCKET_SERVICE': 'MQTT', # 'WS', # 'MQTT'
    # 'WS_SERVER': 'ws://172.21.177.199:8003', # 'ws://172.27.114.105:3210', # 'ws://localhost:3210', #'ws://172.27.114.105:3210', # Needed only if WEBSOCKET_SERVICE is WS
    'WEBSOCKET_SERVICE': 'MQTT',  #'WS', # 'MQTT'
    'WS_SERVER': 'ws://192.168.10.135:8003', # 'ws://localhost:8003', # 'ws://172.27.114.105:3210', # 'ws://localhost:3210', #'ws://172.27.114.105:3210', # Needed only if WEBSOCKET_SERVICE is WS


    # 'SIM_SETTINGS': {
    #     'SIM_DURATION': 480, # 960, # 600,    # 60 # Num Steps
    #     'SIM_STEP_SIZE': 15, # 15, # 6,     # 60   # seconds
    #     'NUMSTEPS_BETWEEN_SOLVER': 1, #2,

    #     'NUM_DRIVERS': 100,       # 100,
    #     'NUM_PASSENGERS': 400,   # 300,

    #     # 'PLANNING_AREA': 'CLEMENTI',
    #     'COVERAGE_AREA': [
    #         # {
    #         #     'name': 'Westside',
    #         #     'districts': ['CLEMENTI', 'JURONG EAST', 'QUEENSTOWN'],
    #         #     'strategy': 'CompromiseMatching',
    #         # },
    #         # {
    #         #     'name': 'NorthEast',
    #         #     'districts': ['PUNGGOL', 'SELETAR', 'HOUGANG'],
    #         #     'strategy': 'CompromiseMatching',
    #         # },
    #         # {
    #         #     'name': 'East',
    #         #     'districts': ['CHANGI', 'PASIR RIS', 'TAMPINES', 'BEDOK'],
    #         #     'strategy': 'CompromiseMatching',
    #         # },
    #         {
    #             'name': 'RoundIsland',
    #             'districts': ['PUNGGOL', 'SELETAR', 'HOUGANG', 'CLEMENTI', 'JURONG EAST', 'QUEENSTOWN'],
    #             'strategy': 'CompromiseMatching',
    #         },
    #         # {
    #         #     'name': 'Singapore',
    #         #     'districts': ['SIMPANG', 'SUNGEI KADUT', 'DOWNTOWN CORE', 'NEWTON', 'ORCHARD', 'KALLANG', 'LIM CHU KANG', 'PASIR RIS',  'MARINA SOUTH', 'SERANGOON', 'BOON LAY', 'BEDOK', 'BUKIT MERAH', 'BUKIT PANJANG', 'JURONG EAST', 'BUKIT TIMAH', 'CHANGI', 'CHOA CHU KANG', 'QUEENSTOWN', 'SELETAR', 'MANDAI', 'ANG MO KIO', 'BISHAN', 'BUKIT BATOK',  'JURONG WEST', 'CLEMENTI', 'GEYLANG', 'HOUGANG', 'PIONEER', 'PUNGGOL', 'SEMBAWANG', 'SENGKANG', 'TAMPINES', 'TANGLIN', 'TOA PAYOH', 'WOODLANDS', 'YISHUN', 'OUTRAM', 'MARINE PARADE', 'NOVENA', 'PAYA LEBAR', 'RIVER VALLEY', 'ROCHOR',],
    #         #     'strategy': 'CompromiseMatching',
    #         # },
    #     ],

    #     'PUBLISH_REALTIME_DATA': True, #True, #False,
    #     'WRITE_WS_OUTPUT_TO_FILE': True,

    #     'PUBLISH_PATHS_HISTORY': False,
    #     'WRITE_PH_OUTPUT_TO_FILE': False,
    #     'PATHS_HISTORY_TIME_WINDOW': 1*30*60, # 900 # seconds

    #     'STEP_TIMEOUT': 60, # Max Compute time for each step (seconds)
    # },
}

orsim_settings = {
    'SIM_DURATION': 480, # 960, # 600,    # 60 # Num Steps
    'SIM_STEP_SIZE': 15, # 15, # 6,     # 60   # seconds

    'STEP_TIMEOUT': 60, # Max Compute time for each step (seconds)
}

analytics_settings = {
    'PUBLISH_REALTIME_DATA': True, #True, #False,
    'WRITE_WS_OUTPUT_TO_FILE': True,

    'PUBLISH_PATHS_HISTORY': False,
    'WRITE_PH_OUTPUT_TO_FILE': False,
    'PATHS_HISTORY_TIME_WINDOW': 1*30*60, # 900 # seconds

}

assignment_settings = {
    'NUMSTEPS_BETWEEN_SOLVER': 1, #2,

    'COVERAGE_AREA': [
        # {
        #     'name': 'Westside',
        #     'districts': ['CLEMENTI', 'JURONG EAST', 'QUEENSTOWN'],
        #     'strategy': 'CompromiseMatching',
        # },
        # {
        #     'name': 'NorthEast',
        #     'districts': ['PUNGGOL', 'SELETAR', 'HOUGANG'],
        #     'strategy': 'CompromiseMatching',
        # },
        # {
        #     'name': 'East',
        #     'districts': ['CHANGI', 'PASIR RIS', 'TAMPINES', 'BEDOK'],
        #     'strategy': 'CompromiseMatching',
        # },
        {
            'name': 'RoundIsland',
            'districts': ['PUNGGOL', 'SELETAR', 'HOUGANG', 'CLEMENTI', 'JURONG EAST', 'QUEENSTOWN'],
            'strategy': 'CompromiseMatching',
        },
        # {
        #     'name': 'Singapore',
        #     'districts': ['SIMPANG', 'SUNGEI KADUT', 'DOWNTOWN CORE', 'NEWTON', 'ORCHARD', 'KALLANG', 'LIM CHU KANG', 'PASIR RIS',  'MARINA SOUTH', 'SERANGOON', 'BOON LAY', 'BEDOK', 'BUKIT MERAH', 'BUKIT PANJANG', 'JURONG EAST', 'BUKIT TIMAH', 'CHANGI', 'CHOA CHU KANG', 'QUEENSTOWN', 'SELETAR', 'MANDAI', 'ANG MO KIO', 'BISHAN', 'BUKIT BATOK',  'JURONG WEST', 'CLEMENTI', 'GEYLANG', 'HOUGANG', 'PIONEER', 'PUNGGOL', 'SEMBAWANG', 'SENGKANG', 'TAMPINES', 'TANGLIN', 'TOA PAYOH', 'WOODLANDS', 'YISHUN', 'OUTRAM', 'MARINE PARADE', 'NOVENA', 'PAYA LEBAR', 'RIVER VALLEY', 'ROCHOR',],
        #     'strategy': 'CompromiseMatching',
        # },
    ],
}

driver_settings = {
    'NUM_DRIVERS': 100,       # 100,
    'BEHAVIOR': 'random',       # 100,
}

passenger_settings = {
    'NUM_PASSENGERS': 400,       # 100,
    'BEHAVIOR': 'random',       # 100,
}

