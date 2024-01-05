import logging, traceback
import requests, json
from http import HTTPStatus
from dateutil.relativedelta import relativedelta

from apps.config import settings
from apps.utils import id_generator, is_success
from apps.state_machine import WorkflowStateMachine

# from apps.utils.user_registry import UserRegistry

class PassengerManager():

    def __init__(self, run_id, sim_clock, user, passenger_profile):
        self.run_id = run_id
        self.user = user

        self.passenger_profile = passenger_profile

        self.passenger = self.init_passenger(sim_clock)

    def as_dict(self):
        return self.passenger

    def get_id(self):
        return self.passenger['_id']

    def estimate_next_event_time(self, current_time):
        ''' Return a distant future data as a placeholder'''
        # Ideally checkdriver status and return a distant future if logged out and a realistic value if logged in
        return current_time + relativedelta(years=1)

    def init_passenger(self, sim_clock):
        passenger_url = f"{settings['OPENRIDE_SERVER_URL']}/{self.run_id}/passenger"

        response = requests.get(passenger_url, headers=self.user.get_headers(), timeout=settings.get('NETWORK_REQUEST_TIMEOUT', 10))

        if is_success(response.status_code):
            if len(response.json()['_items']) == 0:
                # Need to register and actvate passenger
                response = self.create_passenger(sim_clock)
                return self.init_passenger(sim_clock)

            else:
                passenger = response.json()['_items'][0]
        else:
            raise Exception(f"{response.url}, {response.text}")

        return passenger

    def create_passenger(self, sim_clock):
        create_passenger_url = f"{settings['OPENRIDE_SERVER_URL']}/{self.run_id}/passenger"

        data = {
            "profile": self.passenger_profile,
            "sim_clock": sim_clock
        }

        return requests.post(create_passenger_url, headers=self.user.get_headers(), data=json.dumps(data), timeout=settings.get('NETWORK_REQUEST_TIMEOUT', 10))

    def login(self, sim_clock):
        passenger_url = f"{settings['OPENRIDE_SERVER_URL']}/{self.run_id}/passenger"

        if self.passenger['state'] == 'dormant': #'offline':
            machine = WorkflowStateMachine(start_value=self.passenger['state'])
            s = machine.current_state
            for t in s.transitions:
                # if t.destinations[0].name == 'offline': #'offline':
                if t.target.name == 'offline': #'offline':
                    data = {
                        # "transition": t.identifier,
                        "transition": t.event,
                        "sim_clock": sim_clock
                    }
                    passenger_item_url = passenger_url + f"/{self.passenger['_id']}"

                    requests.patch(passenger_item_url,
                                    headers=self.user.get_headers(etag=self.passenger['_etag']),
                                    data=json.dumps(data),
                                    timeout=settings.get('NETWORK_REQUEST_TIMEOUT', 10))

                    response = requests.get(passenger_item_url, headers=self.user.get_headers(), timeout=settings.get('NETWORK_REQUEST_TIMEOUT', 10))
                    self.passenger = response.json()

                    return self.login(sim_clock)
        elif self.passenger['state'] == 'offline': #'offline':
            machine = WorkflowStateMachine(start_value=self.passenger['state'])
            s = machine.current_state
            for t in s.transitions:
                # if t.destinations[0].name == 'online': #'offline':
                if t.target.name == 'online': #'offline':
                    data = {
                        # "transition": t.identifier,
                        "transition": t.event,
                        "sim_clock": sim_clock
                    }

                    passenger_item_url = passenger_url + f"/{self.passenger['_id']}"

                    requests.patch(passenger_item_url,
                                    headers=self.user.get_headers(etag=self.passenger['_etag']),
                                    data=json.dumps(data), timeout=settings.get('NETWORK_REQUEST_TIMEOUT', 10))

                    response = requests.get(passenger_item_url, headers=self.user.get_headers(), timeout=settings.get('NETWORK_REQUEST_TIMEOUT', 10))
                    if is_success(response.status_code):
                        self.passenger = response.json()
                    else:
                        raise Exception(f"{response.url}, {response.text}")

                    return self.login(sim_clock)
        elif self.passenger['state'] == 'online': #'offline':
            return None
        else:
            raise Exception ("unknown Workflow State")

    def logout(self, sim_clock):
        ''' '''
        item_url = f"{settings['OPENRIDE_SERVER_URL']}/{self.run_id}/passenger/{self.get_id()}"

        data = {
            "transition": "logout",
            "sim_clock": sim_clock,
        }

        response = requests.patch(item_url,
                        headers=self.user.get_headers(etag=self.passenger['_etag']),
                        data=json.dumps(data)) # no timeout

        if is_success(response.status_code):
            self.refresh()
        else:
            raise Exception(f"{response.url}, {response.text}")

    def refresh(self):
        passenger_item_url = f"{settings['OPENRIDE_SERVER_URL']}/{self.run_id}/passenger/{self.passenger['_id']}"

        response = requests.get(passenger_item_url, headers=self.user.get_headers(), timeout=settings.get('NETWORK_REQUEST_TIMEOUT', 10))

        if is_success(response.status_code):
            self.passenger = response.json()
        else:
            raise Exception(f'PassengerManager.refresh: Failed getting response for {self.passenger["_id"]} Got {response.text}')
