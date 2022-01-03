import logging

settings = {
    'OPENRIDE_SERVER_URL': 'http://localhost:11654', #'http://192.168.10.135:11654', #'http://127.0.0.1:11654',

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

    'NETWORK_REQUEST_TIMEOUT': 10,      # Seconds

}

# orsim_settings = {
#     'SIMULATION_LENGTH_IN_STEPS': 960, # 960, # 600,    # 60 # Num Steps
#     'STEP_INTERVAL': 30, # 15, # 6,     # 60   # seconds in Simulation Universe

#     'STEP_TIMEOUT': 60, # Max Compute time for each step (seconds) in CPU time
#     'STEP_TIMEOUT_TOLERANCE': 0.05,
# }

# analytics_settings = {
#     'PUBLISH_REALTIME_DATA': False, #True, #False,
#     'WRITE_WS_OUTPUT_TO_FILE': True,

#     'PUBLISH_PATHS_HISTORY': False,
#     'WRITE_PH_OUTPUT_TO_FILE': False,
#     'PATHS_HISTORY_TIME_WINDOW': 1*30*60, # 900 # seconds

#     'STEPS_PER_ACTION': 1, #2,
#     'RESPONSE_RATE': 1, # Keep this 1 to regularly update stats
#     'STEP_ONLY_ON_EVENTS': False,
# }

# assignment_settings = {
#     'STEPS_PER_ACTION': 1, #2,
#     'RESPONSE_RATE': 1,  # Keep this 1 to regularly update stats
#     'STEP_ONLY_ON_EVENTS': False,

#     'COVERAGE_AREA': [
#         # {
#         #     'name': 'Clementi',
#         #     'districts': ['CLEMENTI'],
#         #     'strategy': 'CompromiseMatching', # 'GreedyMinPickupMatching', # 'CompromiseMatching',
#         #     'max_travel_time_pickup': 300
#         # },
#         # {
#         #     'name': 'Westside',
#         #     'districts': ['CLEMENTI', 'JURONG EAST', 'QUEENSTOWN'],
#         #     'strategy': 'CompromiseMatching',
#         #     'max_travel_time_pickup': 300 # seconds
#         # },
#         # {
#         #     'name': 'NorthEast',
#         #     'districts': ['PUNGGOL', 'SELETAR', 'HOUGANG'],
#         #     'strategy': 'CompromiseMatching',
#         #     'max_travel_time_pickup': 300 # seconds
#         # },
#         # {
#         #     'name': 'East',
#         #     'districts': ['CHANGI', 'PASIR RIS', 'TAMPINES', 'BEDOK'],
#         #     'strategy': 'CompromiseMatching',
#         #     'max_travel_time_pickup': 300 # seconds
#         # },
#         # {
#         #     'name': 'RoundIsland',
#         #     'districts': ['PUNGGOL', 'SELETAR', 'HOUGANG', 'CLEMENTI', 'JURONG EAST', 'QUEENSTOWN',  'DOWNTOWN CORE', 'NEWTON', 'ORCHARD', 'KALLANG', 'CHOA CHU KANG', 'MANDAI',],
#         #     'strategy': 'GreedyMinPickupMatching' # 'CompromiseMatching',
#         #     'max_travel_time_pickup': 300 # seconds
#         # },
#         # {
#         #     'name': 'Singapore',
#         #     'districts': ['SIMPANG', 'SUNGEI KADUT', 'DOWNTOWN CORE', 'NEWTON', 'ORCHARD', 'KALLANG', 'LIM CHU KANG', 'PASIR RIS',  'MARINA SOUTH', 'SERANGOON', 'BOON LAY', 'BEDOK', 'BUKIT MERAH', 'BUKIT PANJANG', 'JURONG EAST', 'BUKIT TIMAH', 'CHANGI', 'CHOA CHU KANG', 'QUEENSTOWN', 'SELETAR', 'MANDAI', 'ANG MO KIO', 'BISHAN', 'BUKIT BATOK',  'JURONG WEST', 'CLEMENTI', 'GEYLANG', 'HOUGANG', 'PIONEER', 'PUNGGOL', 'SEMBAWANG', 'SENGKANG', 'TAMPINES', 'TANGLIN', 'TOA PAYOH', 'WOODLANDS', 'YISHUN', 'OUTRAM', 'MARINE PARADE', 'NOVENA', 'PAYA LEBAR', 'RIVER VALLEY', 'ROCHOR',],
#         #     'strategy': 'CompromiseMatching',
#         #     'max_travel_time_pickup': 300 # seconds
#         # },
#         {
#             'name': 'Singapore_SG',
#             'districts': ['SINGAPORE',],
#             'strategy': 'GreedyMinPickupMatching',  #'CompromiseMatching',  # 'RandomAssignment'
#             'max_travel_time_pickup': 600 # seconds
#         },
#     ],
# }

# driver_settings = {
#     'NUM_DRIVERS': 300,       # 100,
#     'BEHAVIOR': 'random',       # 100,

#     'STEPS_PER_ACTION': 1, #2,
#     'RESPONSE_RATE': 1, # 0.25
#     'STEP_ONLY_ON_EVENTS': True,

#     # 'LOCATION_PING_INTERVAL': 15,  # seconds in Simulation Universe
#     # NOTE LOCATION_PING_INTERVAL must be Less than STEP_INTERVAL
#     'UPDATE_PASSENGER_LOCATION': False, # For performance reasons, internallly this is fixed to False
# }

# passenger_settings = {
#     'NUM_PASSENGERS': 4000,       # 100,
#     'BEHAVIOR': 'random',       # 100,

#     'STEPS_PER_ACTION': 1, #2,
#     'RESPONSE_RATE': 1, # 0.25
#     'STEP_ONLY_ON_EVENTS': True
# }

