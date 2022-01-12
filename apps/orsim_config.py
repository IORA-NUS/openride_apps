

orsim_settings = {
    # 'SIMULATION_LENGTH_IN_STEPS': 960, # 960, # 600,    # 60 # Num Steps
    'SIMULATION_LENGTH_IN_STEPS': 960, # 960, # 600,    # 60 # Num Steps
    'STEP_INTERVAL': 30, # 15, # 6,     # 60   # seconds in Simulation Universe

    'AGENT_LAUNCH_TIMEOUT': 15,
    'STEP_TIMEOUT': 60, # Max Compute time for each step (seconds) in CPU time
    'STEP_TIMEOUT_TOLERANCE': 0.1,

    'REFERENCE_TIME': '2020-01-01 04:00:00',
}

analytics_settings = {
    'PUBLISH_REALTIME_DATA': False, #True, #False,
    'WRITE_WS_OUTPUT_TO_FILE': True,

    'PUBLISH_PATHS_HISTORY': False,
    'WRITE_PH_OUTPUT_TO_FILE': False,
    'PATHS_HISTORY_TIME_WINDOW': 1*30*60, # 900 # seconds

    'STEPS_PER_ACTION': 1, #2,
    'RESPONSE_RATE': 1, # Keep this 1 to regularly update stats
    'STEP_ONLY_ON_EVENTS': False,
}

assignment_settings = {
    'STEPS_PER_ACTION': 1, #2,
    'RESPONSE_RATE': 1,  # Keep this 1 to regularly update stats
    'STEP_ONLY_ON_EVENTS': False,

    'COVERAGE_AREA': [
        # {
        #     'name': 'Clementi',
        #     'districts': ['CLEMENTI'],
        #     'strategy': 'CompromiseMatching', # 'GreedyMinPickupMatching', # 'CompromiseMatching',
        #     'max_travel_time_pickup': 300
        #     'online_metric_scale_strategy': 'time', # Allowed: time | demand
        # },
        # {
        #     'name': 'Westside',
        #     'districts': ['CLEMENTI', 'JURONG EAST', 'QUEENSTOWN'],
        #     'strategy': 'CompromiseMatching',
        #     'max_travel_time_pickup': 300 # seconds
        #     'online_metric_scale_strategy': 'time', # Allowed: time | demand
        # },
        # {
        #     'name': 'NorthEast',
        #     'districts': ['PUNGGOL', 'SELETAR', 'HOUGANG'],
        #     'strategy': 'CompromiseMatching',
        #     'max_travel_time_pickup': 300 # seconds
        #     'online_metric_scale_strategy': 'time', # Allowed: time | demand
        # },
        # {
        #     'name': 'East',
        #     'districts': ['CHANGI', 'PASIR RIS', 'TAMPINES', 'BEDOK'],
        #     'strategy': 'CompromiseMatching',
        #     'max_travel_time_pickup': 300 # seconds
        #     'online_metric_scale_strategy': 'time', # Allowed: time | demand
        # },
        # {
        #     'name': 'RoundIsland',
        #     'districts': ['PUNGGOL', 'SELETAR', 'HOUGANG', 'CLEMENTI', 'JURONG EAST', 'QUEENSTOWN',  'DOWNTOWN CORE', 'NEWTON', 'ORCHARD', 'KALLANG', 'CHOA CHU KANG', 'MANDAI',],
        #     'strategy': 'GreedyMinPickupMatching' # 'CompromiseMatching',
        #     'max_travel_time_pickup': 300 # seconds
        #     'online_metric_scale_strategy': 'time', # Allowed: time | demand
        # },
        # {
        #     'name': 'Singapore',
        #     'districts': ['SIMPANG', 'SUNGEI KADUT', 'DOWNTOWN CORE', 'NEWTON', 'ORCHARD', 'KALLANG', 'LIM CHU KANG', 'PASIR RIS',  'MARINA SOUTH', 'SERANGOON', 'BOON LAY', 'BEDOK', 'BUKIT MERAH', 'BUKIT PANJANG', 'JURONG EAST', 'BUKIT TIMAH', 'CHANGI', 'CHOA CHU KANG', 'QUEENSTOWN', 'SELETAR', 'MANDAI', 'ANG MO KIO', 'BISHAN', 'BUKIT BATOK',  'JURONG WEST', 'CLEMENTI', 'GEYLANG', 'HOUGANG', 'PIONEER', 'PUNGGOL', 'SEMBAWANG', 'SENGKANG', 'TAMPINES', 'TANGLIN', 'TOA PAYOH', 'WOODLANDS', 'YISHUN', 'OUTRAM', 'MARINE PARADE', 'NOVENA', 'PAYA LEBAR', 'RIVER VALLEY', 'ROCHOR',],
        #     'strategy': 'CompromiseMatching',
        #     'max_travel_time_pickup': 300, # seconds
        #     'online_metric_scale_strategy': 'time', # Allowed: time | demand
        # },
        # {
        #     'name': 'Singapore_SG',
        #     'districts': ['SINGAPORE',],
        #     'strategy': 'PickupOptimalMatching', #'GreedyMinPickupMatching',  #'CompromiseMatching',  # 'RandomAssignment'
        #     'max_travel_time_pickup': 600, # seconds
        #     'online_metric_scale_strategy': 'time', # Allowed: time | demand
        # },
        {
            'name': 'Changi',
            'districts': ['CHANGI',],
            'strategy': 'PickupOptimalMatching', #'GreedyMinPickupMatching',  #'CompromiseMatching',  # 'RandomAssignment'
            'max_travel_time_pickup': 600, # seconds
            'online_metric_scale_strategy': 'time', # Allowed: time | demand
        },
    ],
}

driver_settings = {
    'NUM_DRIVERS': 10,       # 100,
    'BEHAVIOR': 'random',       # 100,

    'STEPS_PER_ACTION': 1, #2,
    'RESPONSE_RATE': 1, # 0.25
    'STEP_ONLY_ON_EVENTS': True,

    'ACTION_WHEN_FREE': 'random_walk', # 'random_walk', 'stay'

    # 'LOCATION_PING_INTERVAL': 15,  # seconds in Simulation Universe
    # NOTE LOCATION_PING_INTERVAL must be Less than STEP_INTERVAL
    'UPDATE_PASSENGER_LOCATION': False, # For performance reasons, internallly this is fixed to False
}

passenger_settings = {
    'NUM_PASSENGERS': 50,       # 100,
    'BEHAVIOR': 'random',       # 100,

    'STEPS_PER_ACTION': 1, #2,
    'RESPONSE_RATE': 1, # 0.25
    'STEP_ONLY_ON_EVENTS': True
}

