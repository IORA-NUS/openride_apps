
from random import randint, choice
from shapely.geometry import Point, mapping

from apps.loc_service import BusStop, PlanningArea
# from apps.config import settings

from apps.config import (assignment_settings, orsim_settings,
                         driver_settings, passenger_settings,
                         analytics_settings
                        )

class GenerateBehavior():

    stop_locations = {
        coverage_area['name']: BusStop().get_locations_within(coverage_area['districts']) # NOTE need to pass a list
                                            for coverage_area in assignment_settings['COVERAGE_AREA']
    }

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
            # shift_start_time = randint(0, (orsim_settings['SIMULATION_LENGTH_IN_STEPS']//4))
            # shift_end_time = randint(orsim_settings['SIMULATION_LENGTH_IN_STEPS']//2, orsim_settings['SIMULATION_LENGTH_IN_STEPS']-1)
            shift_start_time = randint(0, (orsim_settings['SIMULATION_LENGTH_IN_STEPS']//10))
            shift_end_time = orsim_settings['SIMULATION_LENGTH_IN_STEPS']-1

            coverage_area = choice(assignment_settings['COVERAGE_AREA'])
            coverage_area_name = coverage_area['name']

            init_loc = cls.get_random_location(coverage_area_name)
            empty_dest_loc = cls.get_random_location(coverage_area_name)

            patience = 150
            service_score = randint(1, 1000)

        else:
            shift_start_time = record['Start_Time']
            shift_end_time = record['End_Time']

            coverage_area_name = record['coverage_area_name']

            init_loc = mapping(Point(record["Start_Longitude"], record["Start_Latitude"]))
            empty_dest_loc = mapping(Point(record["End_Longitude"], record["End_Latitude"]))

            patience = 150
            service_score = record['Service_Quality']


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

            'TrTime_pickup': 0, # NOTE This should be embedded in Passenger behavior (may recieve this via message or requested_trip dict?)
            'TrTime_dropoff': 0,
        }

        return behavior

    @classmethod
    def ridehail_passenger(cls, id, record=None):

        if record is None:
            trip_request_time = randint(0, orsim_settings['SIMULATION_LENGTH_IN_STEPS']-1)

            coverage_area = choice(assignment_settings['COVERAGE_AREA'])
            coverage_area_name = coverage_area['name']

            pickup_loc = cls.get_random_location(coverage_area_name)
            dropoff_loc = cls.get_random_location(coverage_area_name)

            trip_price = randint(0, 100)
            patience = 600


        else:
            trip_request_time = record['trip_request_time']

            pickup_loc = mapping(Point(record["Start_Longitude"], record["Start_Latitude"]))
            dropoff_loc = mapping(Point(record["End_Longitude"], record["End_Latitude"]))

            trip_price = record["Fare"]

            patience = record['Patience_Level']


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
        }

        return behavior

    @classmethod
    def ridehail_analytics(cls, id, record=None):

        behavior = {
            'email': f'{id}@test.com',
            'password': 'password',
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

                'offline_params': {
                    'reverseParameter': 480,  # 480;
                    'reverseParameter2': 2.5,
                    'gamma': 1.2,     # the target below is estimated from historical data

                    # KPI Targets
                    'targetReversePickupTime': 4915 * 1.2, # gamma
                    'targetServiceScore': 5439 * 1.2, # gamma
                    'targetRevenue': 4185 * 1.2, # gamma
                },
                'online_params': {
                    'realtimePickupTime': 0,
                    'realtimeRevenue': 0,
                    'realtimeServiceScore': 0,

                    'weightPickupTime': 1,
                    'weightRevenue': 1,
                    'weightServiceScore': 1,
                },
            },
        }

        return behavior

