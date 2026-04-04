from dateutil.relativedelta import relativedelta
import json, logging

from apps.config import settings, simulation_domains
from apps.utils import is_success, str_to_time

from apps.ridehail.statemachine import RidehailDriverTripStateMachine
from apps.utils import time_to_str, str_to_time
from apps.common.trip_manager_base import TripManagerBase
from apps.ridehail.statemachine import RideHailActions, RideHailEvents

from apps.utils.excepions import WriteFailedException, RefreshException
from apps.loc_service import OSRMClient

from apps.ridehail.statemachine.driver_passenger_interactions import driver_passenger_interactions
from apps.ridehail.message_data_models import AssignedActionPayload

class DriverTripManager(TripManagerBase):
    ''' '''
    # trip = None

    def __init__(self, run_id, sim_clock, user, messenger, update_passenger_loc=False, persona=None):
        super().__init__(run_id, user, messenger, persona=persona)
        self.update_passenger_loc = update_passenger_loc
        self.simulation_domain = simulation_domains['ridehail']

    @property
    def StateMachineCls(self):
        return RidehailDriverTripStateMachine

    @property
    def message_channel(self):
        return f'{self.run_id}/{self.trip["passenger"]}'

    @property
    def statemachine_interaction_mapping(self):
        return driver_passenger_interactions

    def message_template(self, event):
        # NOTE This message template is critical. Ensure the action, self recognition and data with event is included
        return {
            'action': RideHailActions.DRIVER_WORKFLOW_EVENT,
            'driver_id': self.trip.get('driver'),
            'data': {
                'event': event
            }
        }


    def as_dict(self):
        return self.trip

    def estimate_next_event_time(self, current_time):
        '''
        current_time is datetime
        '''
        try:
            if self.trip['state'] in RidehailDriverTripStateMachine.driver_looking_for_job.name:
                try:
                    trip_duration = self.trip['routes']['planned']['looking_for_job']['duration']
                except Exception as e:
                    logging.debug(str(e))
                    trip_duration = 0

                try:
                    last_waypoint_time = str_to_time(self.trip['last_waypoint']['_updated']) # May use sim_clock for consistency
                    # NOTE May need to check if elapsed time since state change needs to be computed instead of using last_waypoint_time
                except Exception as e:
                    logging.debug(str(e))
                    last_waypoint_time = current_time

                next_waypoint_time = max((last_waypoint_time + relativedelta(seconds=trip_duration)), current_time)

            elif self.trip['state'] == RidehailDriverTripStateMachine.driver_moving_to_pickup.name:
                try:
                    trip_duration = self.trip['routes']['planned']['moving_to_pickup']['duration']
                except Exception as e:
                    logging.debug(str(e))
                    trip_duration = 0

                try:
                    last_waypoint_time = str_to_time(self.trip['last_waypoint']['_updated']) # May use sim_clock for consistency
                    # NOTE May need to check if elapsed time since state change needs to be computed instead of using last_waypoint_time
                except Exception as e:
                    logging.debug(str(e))
                    last_waypoint_time = current_time

                next_waypoint_time = max((last_waypoint_time + relativedelta(seconds=trip_duration)), current_time)

            elif self.trip['state'] == RidehailDriverTripStateMachine.driver_moving_to_dropoff.name:
                try:
                    trip_duration = self.trip['routes']['planned']['moving_to_dropoff']['duration']
                except Exception as e:
                    logging.debug(str(e))
                    trip_duration = 0

                try:
                    last_waypoint_time = str_to_time(self.trip['last_waypoint']['_updated']) # May use sim_clock for consistency
                    # NOTE May need to check if elapsed time since state change needs to be computed instead of using last_waypoint_time
                except Exception as e:
                    logging.debug(str(e))
                    last_waypoint_time = current_time

                next_waypoint_time = max((last_waypoint_time + relativedelta(seconds=trip_duration)), current_time)

            else:
                next_waypoint_time = current_time
        except:
            next_waypoint_time = current_time

        return next_waypoint_time

    # def create_new_unoccupied_trip(self, sim_clock, current_loc, driver, vehicle):
    def create_new_unoccupied_trip(self, sim_clock, current_loc, driver, vehicle, route):
        data = {
            "driver": f"{driver['_id']}",
            'persona': self.persona,
            "meta": {
                'profile': driver['profile'],
            },
            "vehicle": f"{vehicle['_id']}",
            "current_loc": current_loc,
            "next_dest_loc": current_loc,
            "is_occupied": False,
            "statemachine": {
                "name": "RidehailDriverTripStateMachine",
                "domain": self.simulation_domain,
            },
            "state": RidehailDriverTripStateMachine.initial_state.name,
            "sim_clock": sim_clock,
        }

        response = self._post_trip(data)

        if is_success(response.status_code):
            self.trip = {'_id': response.json()['_id']}
            self.refresh()
            self.look_for_job(sim_clock, current_loc, route)
        else:
            raise WriteFailedException(f"{response.url}, {response.text}")

    def create_new_occupied_trip(self, sim_clock, current_loc, driver, vehicle, ridehail_passenger_trip):
        data = {
            "driver": f"{driver['_id']}",
            'persona': self.persona,
            "meta": {
                'profile': driver['profile'],
            },
            "vehicle": f"{vehicle['_id']}",
            "current_loc": current_loc,
            "next_dest_loc": ridehail_passenger_trip['pickup_loc'],
            "ridehail_passenger_trip": ridehail_passenger_trip['_id'],
            "passenger": ridehail_passenger_trip['passenger'],
            "trip_start_loc": current_loc,
            "pickup_loc": ridehail_passenger_trip['pickup_loc'],
            "dropoff_loc": ridehail_passenger_trip['dropoff_loc'],
            "is_occupied": True,
            "statemachine": {
                "name": "RidehailDriverTripStateMachine",
                "domain": self.simulation_domain,
            },
            "state": RidehailDriverTripStateMachine.initial_state.name,
            "sim_clock": sim_clock,
        }


        response = self._post_trip(data)

        if is_success(response.status_code):
            self.trip = {'_id': response.json()['_id']}
            self.refresh()
            self.recieve(sim_clock, current_loc)
        else:
            raise WriteFailedException(f"{response.url}, {response.text}")

    def look_for_job(self, sim_clock, current_loc, route):

        data = {
            'sim_clock': sim_clock,
            'current_loc': current_loc,
            'routes.planned.looking_for_job': route,
        }

        response = self._patch_trip_transition('look_for_job', data)

        if is_success(response.status_code):
            self.refresh()

        else:
            raise WriteFailedException(f"{response.url}, {response.text}")


    def recieve(self, sim_clock, current_loc):

        data = {
            'sim_clock': sim_clock,
            'current_loc': current_loc
        }

        response = self._patch_trip_transition('recieve', data)

        if is_success(response.status_code):
            self.refresh()

            msg = AssignedActionPayload(
                action=RideHailActions.ASSIGNED, # NOTE This is not a DriverWorkflowEvent, but a direct message to passenger to trigger pickup workflow.
                driver_id=self.trip['driver']
            )
            self.messenger.client.publish(
                f'{self.run_id}/{self.trip["passenger"]}',
                json.dumps(msg.__dict__)
            )

        else:
            raise WriteFailedException(f"Failed sending Assigned message to passenger after receiving trip: {response.url}, {response.text}")


    def end_active_trip(self, sim_clock, current_loc, force=False):
        active_trip = self.as_dict()
        if active_trip is not None:
            if active_trip['is_occupied'] == False:
                self.apply_trip_transition_and_notify(
                    transition=RidehailDriverTripStateMachine.end_trip.name,
                    data={
                        'sim_clock': sim_clock,
                        'current_loc': current_loc
                    },
                    context={}
                )
                return
            if force:
                logging.warning(f'Force quitting active trip. Active trip is occupied but force quit enabled.')
                self.apply_trip_transition_and_notify(
                    transition=RidehailDriverTripStateMachine.force_quit.name,
                    data={
                        'sim_clock': sim_clock,
                        'current_loc': current_loc
                    },
                    context={}
                )
            else:
                logging.warning(f'Cannot end active trip. Active trip is occupied and force quit not enabled.')
        else:
            logging.warning(f'No active trip to end')



    def ping(self, sim_clock, current_loc, **kwargs):
        ''' '''
        if self.trip is None:
            raise Exception('trip is not set')

        data = kwargs
        data['sim_clock'] = sim_clock
        data['current_loc'] = current_loc

        response = self._patch_trip(data)

        if is_success(response.status_code):
            self.refresh()
        else:
            raise WriteFailedException(f"{response.url}, {response.text}")
            # logging.exception(f"Unable to Ping: {response.text}")

    def refresh(self, project=None): #, from_server=True):
        if self.trip is not None: # and from_server:
            response = self._get_trip()

            if is_success(response.status_code):
                self.trip = response.json()
            else:
                raise RefreshException(f'DriverTripManager.refresh: Failed getting response for {self.trip["_id"]} Got {response.text}')

