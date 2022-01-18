
analytics_settings = {
    'publish_realtime_data': False, #True, #False,
    'write_ws_output_to_file': True,

    'publish_paths_history': False,
    'write_ph_output_to_file': False,
    'paths_history_time_window': 1*30*60, # 900 # seconds

    'steps_per_action': 1, #2,
    'response_rate': 1, # Keep this 1 to regularly update stats
    'step_only_on_events': False,
}

assignment_settings = {
    'steps_per_action': 1, #2,
    'response_rate': 1,  # Keep this 1 to regularly update stats
    'step_only_on_events': False,

    'coverage_area': [
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
    'num_drivers': 10,       # 100,
    # 'BEHAVIOR': 'random',       # 100,

    'steps_per_action': 1, #2,
    'response_rate': 1, # 0.25
    'step_only_on_events': True,

    'action_when_free': 'random_walk', # 'random_walk', 'stay'

    # 'LOCATION_PING_INTERVAL': 15,  # seconds in Simulation Universe
    # NOTE LOCATION_PING_INTERVAL must be Less than STEP_INTERVAL
    'update_passenger_location': False, # For performance reasons, internallly this is fixed to False
}

passenger_settings = {
    'num_passengers': 50,       # 100,
    # 'BEHAVIOR': 'random',       # 100,

    'steps_per_action': 1, #2,
    'response_rate': 1, # 0.25
    'step_only_on_events': True
}

