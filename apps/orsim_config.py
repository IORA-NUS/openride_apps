

orsim_settings = {
    'DOMAIN': 'UPDATE_DOMAIN_WHEN_GENERATING_BEHAVIOR',

    # 'SIMULATION_LENGTH_IN_STEPS': 960, # 960, # 600,    # 60 # Num Steps
    'SIMULATION_LENGTH_IN_STEPS': 600, #960, # 960, # 600,    # 60 # Num Steps
    'STEP_INTERVAL': 30, # 15, # 6,     # 60   # seconds in Simulation Universe

    'AGENT_LAUNCH_TIMEOUT': 15,
    'STEP_TIMEOUT': 60, # Max Compute time for each step (seconds) in CPU time
    'STEP_TIMEOUT_TOLERANCE': 0.1,
    # Once the unresponsive fraction is within STEP_TIMEOUT_TOLERANCE and this many
    # seconds have elapsed, stop waiting the full STEP_TIMEOUT — the straggler agents
    # are pruned and the step continues. Keeps a single slow agent from costing 30-60s.
    'STEP_SETTLE_SECONDS': 2,
    'HEARTBEAT_INTERVAL': 5, # seconds

    # Post-horizon drain (paired with ALLOW_POST_HORIZON_DRAIN + HorizonDrainTermination).
    # Orders that can never be served must terminalize so the agent scheduler drains and the
    # run ends, instead of looping forever (see CLAUDE.md §6.4).
    #   POST_HORIZON_GRACE_STEPS: at/after the horizon, unassigned/created orders cancel
    #     immediately; already-assigned/in-flight orders get this many extra steps to finish
    #     (trucks complete their current trip before going offline), after which any still
    #     non-terminal order is force-cancelled.
    #   POST_HORIZON_DRAIN_MAX_STEPS: hard cap — once the run is this many steps past the
    #     horizon, HorizonDrainTermination ends it regardless of remaining agents (backstop
    #     against any stuck agent). Keep > POST_HORIZON_GRACE_STEPS so clean self-cancel wins.
    'POST_HORIZON_GRACE_STEPS': 60,
    'POST_HORIZON_DRAIN_MAX_STEPS': 90,

    'REFERENCE_TIME': '2020-01-01 04:00:00',

    # perf_stream — step_tick every step; step_detail sampling interval
    'PERF_TICK_EVERY_STEP': True,
    'PERF_DETAIL_INTERVAL_STEPS': 10,
    'PERF_DETAIL_ON_NEW_MAX': True,
    'PERF_SLOW_AGENT_TOP_N': 10,
    'PERF_INCLUDE_PROCESS_METRICS': False,
    'PERF_KAFKA_FLUSH_EVERY_STEPS': 10,
    'PERF_KAFKA_INTERVAL_STEPS': 10,
    'RUNTIME_STATUS_UPDATE_INTERVAL_STEPS': 10,
    # Publish a run_status "step X/total" heartbeat to Kafka every N steps so the
    # dashboard progress counter advances during the run (0 disables the heartbeat).
    'KAFKA_HEARTBEAT_INTERVAL_STEPS': 5,
}

# analytics_settings = {
#     'publish_realtime_data': False, #True, #False,
#     'write_ws_output_to_file': True,

#     'publish_paths_history': False,
#     'write_ph_output_to_file': False,
#     'paths_history_time_window': 1*30*60, # 900 # seconds

#     'steps_per_action': 1, #2,
#     'response_rate': 1, # Keep this 1 to regularly update stats
#     'step_only_on_events': False,
# }

# assignment_settings = {
#     'steps_per_action': 1, #2,
#     'response_rate': 1,  # Keep this 1 to regularly update stats
#     'step_only_on_events': False,

#     'coverage_area': [
#         # {
#         #     'name': 'Clementi',
#         #     'districts': ['CLEMENTI'],
#         #     'strategy': 'CompromiseMatching', # 'GreedyMinPickupMatching', # 'CompromiseMatching',
#         #     'max_travel_time_pickup': 300
#         #     'online_metric_scale_strategy': 'time', # Allowed: time | demand
#         # },
#         # {
#         #     'name': 'Westside',
#         #     'districts': ['CLEMENTI', 'JURONG EAST', 'QUEENSTOWN'],
#         #     'strategy': 'CompromiseMatching',
#         #     'max_travel_time_pickup': 300 # seconds
#         #     'online_metric_scale_strategy': 'time', # Allowed: time | demand
#         # },
#         # {
#         #     'name': 'NorthEast',
#         #     'districts': ['PUNGGOL', 'SELETAR', 'HOUGANG'],
#         #     'strategy': 'CompromiseMatching',
#         #     'max_travel_time_pickup': 300 # seconds
#         #     'online_metric_scale_strategy': 'time', # Allowed: time | demand
#         # },
#         # {
#         #     'name': 'East',
#         #     'districts': ['CHANGI', 'PASIR RIS', 'TAMPINES', 'BEDOK'],
#         #     'strategy': 'CompromiseMatching',
#         #     'max_travel_time_pickup': 300 # seconds
#         #     'online_metric_scale_strategy': 'time', # Allowed: time | demand
#         # },
#         # {
#         #     'name': 'RoundIsland',
#         #     'districts': ['PUNGGOL', 'SELETAR', 'HOUGANG', 'CLEMENTI', 'JURONG EAST', 'QUEENSTOWN',  'DOWNTOWN CORE', 'NEWTON', 'ORCHARD', 'KALLANG', 'CHOA CHU KANG', 'MANDAI',],
#         #     'strategy': 'GreedyMinPickupMatching' # 'CompromiseMatching',
#         #     'max_travel_time_pickup': 300 # seconds
#         #     'online_metric_scale_strategy': 'time', # Allowed: time | demand
#         # },
#         # {
#         #     'name': 'Singapore',
#         #     'districts': ['SIMPANG', 'SUNGEI KADUT', 'DOWNTOWN CORE', 'NEWTON', 'ORCHARD', 'KALLANG', 'LIM CHU KANG', 'PASIR RIS',  'MARINA SOUTH', 'SERANGOON', 'BOON LAY', 'BEDOK', 'BUKIT MERAH', 'BUKIT PANJANG', 'JURONG EAST', 'BUKIT TIMAH', 'CHANGI', 'CHOA CHU KANG', 'QUEENSTOWN', 'SELETAR', 'MANDAI', 'ANG MO KIO', 'BISHAN', 'BUKIT BATOK',  'JURONG WEST', 'CLEMENTI', 'GEYLANG', 'HOUGANG', 'PIONEER', 'PUNGGOL', 'SEMBAWANG', 'SENGKANG', 'TAMPINES', 'TANGLIN', 'TOA PAYOH', 'WOODLANDS', 'YISHUN', 'OUTRAM', 'MARINE PARADE', 'NOVENA', 'PAYA LEBAR', 'RIVER VALLEY', 'ROCHOR',],
#         #     'strategy': 'CompromiseMatching',
#         #     'max_travel_time_pickup': 300, # seconds
#         #     'online_metric_scale_strategy': 'time', # Allowed: time | demand
#         # },
#         # {
#         #     'name': 'Singapore_SG',
#         #     'districts': ['SINGAPORE',],
#         #     'strategy': 'PickupOptimalMatching', #'GreedyMinPickupMatching',  #'CompromiseMatching',  # 'RandomAssignment'
#         #     'max_travel_time_pickup': 600, # seconds
#         #     'online_metric_scale_strategy': 'time', # Allowed: time | demand
#         # },
#         {
#             'name': 'Changi',
#             'districts': ['CHANGI',],
#             'strategy': 'PickupOptimalMatching', #'GreedyMinPickupMatching',  #'CompromiseMatching',  # 'RandomAssignment'
#             'max_travel_time_pickup': 600, # seconds
#             'online_metric_scale_strategy': 'time', # Allowed: time | demand
#         },
#     ],
# }

# driver_settings = {
#     'num_drivers': 10,       # 100,
#     # 'BEHAVIOR': 'random',       # 100,

#     'steps_per_action': 1, #2,
#     'response_rate': 1, # 0.25
#     'step_only_on_events': True,

#     'action_when_free': 'random_walk', # 'random_walk', 'stay'

#     # 'LOCATION_PING_INTERVAL': 15,  # seconds in Simulation Universe
#     # NOTE LOCATION_PING_INTERVAL must be Less than STEP_INTERVAL
#     'update_passenger_location': False, # For performance reasons, internallly this is fixed to False
# }

# passenger_settings = {
#     'num_passengers': 50,       # 100,
#     # 'BEHAVIOR': 'random',       # 100,

#     'steps_per_action': 1, #2,
#     'response_rate': 1, # 0.25
#     'step_only_on_events': True
# }

