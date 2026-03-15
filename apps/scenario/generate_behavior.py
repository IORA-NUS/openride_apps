
from random import randint, choice, random
from shapely.geometry import Point, mapping
from numpy.random import default_rng


from apps.loc_service import BusStop, PlanningArea

from apps.config import settings
# from apps.orsim_config import driver_settings, passenger_settings, analytics_settings, assignment_settings, orsim_settings
from apps.scenario.scenario_config import driver_settings, passenger_settings, analytics_settings, assignment_settings
from apps.orsim_config import orsim_settings

import haversine as hs

class GenerateBehavior():

    stop_locations = {
        coverage_area['name']: BusStop().get_locations_within(coverage_area['districts']) # NOTE need to pass a list
                                            for coverage_area in assignment_settings['coverage_area']
    }
    rng = default_rng()

    @classmethod
    def get_random_location(cls, coverage_area_name):
        # print(coverage_area_name, cls.stop_locations[coverage_area_name])
        # loc = choice(cls.stop_locations[coverage_area_name])
        # print(loc)
        # return mapping(loc)
        return mapping(choice(cls.stop_locations[coverage_area_name]))

    @classmethod
    def ridehail_driver(cls, id, record=None):

        if record is None:
            # # shift_start_time = randint(0, (orsim_settings['SIMULATION_LENGTH_IN_STEPS']//4))
            # # shift_end_time = randint(orsim_settings['SIMULATION_LENGTH_IN_STEPS']//2, orsim_settings['SIMULATION_LENGTH_IN_STEPS']-1)
            # shift_start_time = randint(0, (orsim_settings['SIMULATION_LENGTH_IN_STEPS']//10))
            shift_start_time = 0
            shift_end_time = orsim_settings['SIMULATION_LENGTH_IN_STEPS']-1

            coverage_area = choice(assignment_settings['coverage_area'])
            coverage_area_name = coverage_area['name']

            init_loc = cls.get_random_location(coverage_area_name)
            empty_dest_loc = cls.get_random_location(coverage_area_name)

            patience = 1500 # 150
            # service_score = randint(1, 1000)
            service_score = 100 * cls.rng.weibull(5)

        else:
            shift_start_time = record['start_time_step'] # Convert to steps... Need reference time
            shift_end_time = record['end_time_step']

            coverage_area_name = record['coverage_area']

            init_loc = mapping(Point(record["start_lon"], record["start_lat"]))
            empty_dest_loc = mapping(Point(record["end_lon"], record["end_lat"]))

            patience = 1500 # 150
            service_score = record['service_score']


        behavior = {
            'email': f'{id}@test.com',
            'password': 'password',

            'shift_start_time': shift_start_time,
            'shift_end_time': shift_end_time,

            'coverage_area_name': coverage_area_name,
            'init_loc': init_loc,
            'empty_dest_loc': empty_dest_loc,

            'profile': {
                'market': 'RideHail',
                'patience': patience,
                'service_score': service_score,
            },

            'transition_prob': [
                [('confirm', 'driver_received_trip'), 1.0],
                [('reject', 'driver_received_trip'), 0.0],
                [('cancel', 'driver_accepted_trip', 'exceeded_patience'), 1.0],
                [('cancel', 'driver_accepted_trip'), 0.0],
                [('cancel', 'driver_moving_to_pickup'), 0.0],
                [('wait_to_pickup', 'driver_moving_to_pickup'), 1.0],
                [('cancel', 'driver_waiting_to_pickup', 'exceeded_patience'), 1.0],
                [('cancel', 'driver_waiting_to_pickup'), 0.0],
            ],

            'transition_time_pickup': 0, # NOTE This should be embedded in Passenger behavior (may recieve this via message or requested_trip dict?)
            'transition_time_dropoff': 0,

            'steps_per_action': driver_settings['steps_per_action'],
            'response_rate': driver_settings['response_rate'],
            'step_only_on_events': driver_settings['step_only_on_events'],
            'update_passenger_location': driver_settings['update_passenger_location'],
            'action_when_free': driver_settings['action_when_free']
        }

        return behavior

    @classmethod
    def ridehail_passenger(cls, id, record=None):

        if record is None:
            trip_request_time = randint(0, orsim_settings['SIMULATION_LENGTH_IN_STEPS']-1)

            coverage_area = choice(assignment_settings['coverage_area'])
            coverage_area_name = coverage_area['name']

            pickup_loc = cls.get_random_location(coverage_area_name)
            dropoff_loc = cls.get_random_location(coverage_area_name)

            # trip_price = randint(0, 100)
            trip_price = max(3, hs.haversine(reversed(pickup_loc['coordinates'][:2]), reversed(dropoff_loc['coordinates'][:2]), unit=hs.Unit.KILOMETERS)) + random()
            patience = 6000 # 600


        else:
            trip_request_time = record['trip_request_time']

            pickup_loc = mapping(Point(record["start_lon"], record["start_lat"]))
            dropoff_loc = mapping(Point(record["end_lon"], record["end_lat"]))

            trip_price = record["trip_price"]

            patience = record['patience']


        behavior = {
            'email': f'{id}@test.com',
            'password': 'password',

            'trip_request_time': trip_request_time, # in units of Simulation Step Size

            'pickup_loc': pickup_loc,
            'dropoff_loc': dropoff_loc,
            'trip_price': trip_price,

            'profile':{
                'market': 'RideHail',
                'patience': patience, # in Seconds
            },

            'transition_prob': [
                # cancel | passenger_requested_trip = 1 if exceeded_patience
                # cancel | passenger_requested_trip ~ 0
                [('cancel', 'passenger_requested_trip', 'exceeded_patience'), 1.0],
                [('cancel', 'passenger_requested_trip'), 0.0],

                # cancel | passenger_assigned_trip ~ 0
                [('cancel', 'passenger_assigned_trip'), 0.0],

                # (accept + reject + cancel) | passenger_received_trip_confirmation == 1
                [('accept', 'passenger_received_trip_confirmation',), 1.0],
                [('reject', 'passenger_received_trip_confirmation'), 0.0],
                [('cancel', 'passenger_received_trip_confirmation'), 0.0],
                [('cancel', 'passenger_received_trip_confirmation', 'exceeded_patience'), 1.0],

                # (cancel + move_for_pickup + wait_for_pickup) | passenger_accepted_trip ~ 0
                [('cancel', 'passenger_accepted_trip'), 0.0],
                # NOTE move_for_pickup and wait_for_pickup transition dependant on currentLoc and PickupLoc

                # cancel | passenger_moving_for_pickup ~ 0
                [('cancel', 'passenger_moving_for_pickup'), 0.0],

                # cancel | passenger_waiting_for_pickup ~ 0
                [('cancel', 'passenger_waiting_for_pickup'), 0.0],

                # end_trip | passenger_droppedoff = 1
                [('end_trip', 'passenger_droppedoff'), 1.0],
            ],

            'steps_per_action': passenger_settings['steps_per_action'],
            'response_rate': passenger_settings['response_rate'],
            'step_only_on_events': passenger_settings['step_only_on_events'],

        }

        return behavior

    @classmethod
    def ridehail_analytics(cls, id, record=None):

        behavior = {
            'email': f'{id}@test.com',
            'password': 'password',

            'steps_per_action': analytics_settings['steps_per_action'],
            'response_rate': analytics_settings['response_rate'],
            'step_only_on_events': analytics_settings['step_only_on_events'],

            'publish_realtime_data': analytics_settings['publish_realtime_data'],
            'write_ws_output_to_file': analytics_settings['write_ws_output_to_file'],

            'publish_paths_history': analytics_settings['publish_paths_history'],
            'write_ph_output_to_file': analytics_settings['write_ph_output_to_file'],
            'paths_history_time_window': analytics_settings['paths_history_time_window'],

        }

        return behavior

    @classmethod
    def ridehail_assignment(cls, id, coverage_area, record=None):
        behavior = {
            'email': f'{id}@test.com',
            'password': 'password',

            'solver': coverage_area['strategy'],


            'solver_params': {
                'planning_area':{
                    'name': coverage_area['name'],
                    'geometry': mapping(PlanningArea().get_planning_area_geometry(coverage_area['districts'])),
                },

                'online_metric_scale_strategy': coverage_area['online_metric_scale_strategy'],
                'max_travel_time_pickup': coverage_area.get('max_travel_time_pickup', 600), # NOTE This is in sync with reverse paremater for compromise matching

                "offline_params": {
                    "ub_pickup_time": 600,

                    "scale_factor_revenue": 1,
                    "scale_factor_reverse_pickup_time": 1,
                    "scale_factor_service_score": 1,

                    "target_revenue": 0, # including gamma
                    "target_reverse_pickup_time": 0, # including gamma
                    "target_service_score": 0, # including gamma
                },
                "online_params": {
                    "realtime_reverse_pickup_time_cum": 0,
                    "realtime_revenue_cum": 0,
                    "realtime_service_score_cum": 0,

                    "weight_pickup_time": 1,
                    "weight_revenue": 1,
                    "weight_service_score": 1
                },

                # 'offline_params': {
                #     'pickupTime_UpperBound': 480,  # 480; # TO be used as a modleing trick to convert pickuptime into a maximization objective max(UpperBound - pickuptime)
                #     # 'reverseParameter2': 1, # 2.5,
                #     # 'gamma': 1.2,     # the target below is estimated from historical data

                #     # KPI Targets
                #     'targetReversePickupTime': 4915, #* 1.2, # gamma
                #     'targetServiceScore': 5439, #* 1.2, # gamma
                #     'targetRevenue': 4185, #* 1.2, # gamma

                #     'reversePickuptime_ScaleFactor': 1,
                #     'revenue_ScaleFactor': 1,
                #     'serviceScore_ScaleFactor': 1,
                # },
                # 'online_params': {
                #     'realtimePickupTime': 0,
                #     'realtimeRevenue': 0,
                #     'realtimeServiceScore': 0,

                #     'weightPickupTime': 1,
                #     'weightRevenue': 1,
                #     'weightServiceScore': 1,
                # },
            },
            'steps_per_action': assignment_settings['steps_per_action'],
            'response_rate': assignment_settings['response_rate'],
            'step_only_on_events': assignment_settings['step_only_on_events'],

        }

        return behavior

