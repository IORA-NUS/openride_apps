
from random import randint, choice
from shapely.geometry import Point, mapping

from apps.loc_service import BusStop, PlanningArea
from apps.config import settings

class BehaviorGen():

    sim_settings = settings['SIM_SETTINGS']
    stop_locations = BusStop().get_locations_within(sim_settings['PLANNING_AREA']) # NOTE THIS CAN A MEMORY HOG. FIND A BETTER SOLUTION


    @classmethod
    def get_random_location(cls):
        return mapping(choice(cls.stop_locations))

    @classmethod
    def ridehail_driver(cls, id, record=None):

        if record is None:
            shift_start_time = randint(0, (cls.sim_settings['SIM_DURATION']//4))
            shift_end_time = randint(cls.sim_settings['SIM_DURATION']//2, cls.sim_settings['SIM_DURATION']-1)

            init_loc = cls.get_random_location()
            empty_dest_loc = cls.get_random_location()

            patience = 150
            service_score = randint(1, 1000)

        else:
            shift_start_time = record['Start_Time']
            shift_end_time = record['End_Time']

            init_loc = mapping(Point(record["Start_Longitude"], record["Start_Latitude"]))
            empty_dest_loc = mapping(Point(record["End_Longitude"], record["End_Latitude"]))

            patience = 150
            service_score = record['Service_Quality']


        behavior = {
            'email': f'{id}@test.com',
            'password': 'password',

            'shift_start_time': shift_start_time,
            'shift_end_time': shift_end_time,

            'init_loc': init_loc,
            'empty_dest_loc': empty_dest_loc,

            'settings': {
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

            # # 'waiting_time_for_pickup': 0, # NOTE This should be embedded in Passenger behavior (may recieve this via message or requested_trip dict?)
            'TrTime_pickup': 0, # NOTE This should be embedded in Passenger behavior (may recieve this via message or requested_trip dict?)
            # # 'waiting_time_for_dropoff': 0, # NOTE This should be embedded in Passenger behavior (may recieve this via message or requested_trip dict?)
            'TrTime_dropoff': 0,
        }

        return behavior

    @classmethod
    def ridehail_passenger(cls, id, record=None):

        if record is None:
            trip_request_time = randint(0, cls.sim_settings['SIM_DURATION']-1)

            pickup_loc = cls.get_random_location()
            dropoff_loc = cls.get_random_location()

            trip_value = randint(0, 100)
            patience = 600


        else:
            trip_request_time = record['trip_request_time']

            pickup_loc = mapping(Point(record["Start_Longitude"], record["Start_Latitude"]))
            dropoff_loc = mapping(Point(record["End_Longitude"], record["End_Latitude"]))

            trip_value = record["Fare"]

            patience = record['Patience_Level']


        behavior = {
            'email': f'{id}@test.com',
            'password': 'password',

            'trip_request_time': trip_request_time, # in units of Simulation Step Size

            'pickup_loc': pickup_loc,
            'dropoff_loc': dropoff_loc,

            'trip_value': trip_value,

            'settings':{
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
    def ridehail_assignment(cls, id, record=None):

        behavior = {
            'email': f'{id}@test.com',
            'password': 'password',

            # 'solver': 'RandomAssignment',
            'solver': 'CompromiseMatching',

            'solver_params': {
                'name': cls.sim_settings['PLANNING_AREA'],
                'area': mapping(PlanningArea().get_planning_area(cls.sim_settings['PLANNING_AREA'])),

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

