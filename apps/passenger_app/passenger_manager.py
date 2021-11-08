import logging
import requests, json
from http import HTTPStatus

from apps.config import settings
from apps.utils import id_generator, is_success
from apps.state_machine import WorkflowStateMachine

from apps.utils.user_registry import UserRegistry

class PassengerManager():

    def __init__(self, run_id, sim_clock, user, passenger_settings):
        self.run_id = run_id
        self.user = user

        self.passenger_settings = passenger_settings

        self.passenger = self.init_passenger(sim_clock)

    def as_dict(self):
        return self.passenger

    def get_id(self):
        return self.passenger['_id']

    def init_passenger(self, sim_clock):
        passenger_url = f"{settings['OPENRIDE_SERVER_URL']}/{self.run_id}/passenger"

        response = requests.get(passenger_url, headers=self.user.get_headers())

        if len(response.json()['_items']) == 0:
            # Need to register and actvate passenger
            response = self.create_passenger(sim_clock)
            return self.init_passenger(sim_clock)

        else:
            passenger = response.json()['_items'][0]

        return passenger

    def create_passenger(self, sim_clock):
        create_passenger_url = f"{settings['OPENRIDE_SERVER_URL']}/{self.run_id}/passenger"

        data = {
            "settings": self.passenger_settings,
            "sim_clock": sim_clock
        }

        return requests.post(create_passenger_url, headers=self.user.get_headers(), data=json.dumps(data))

    def login(self, sim_clock):
        passenger_url = f"{settings['OPENRIDE_SERVER_URL']}/{self.run_id}/passenger"

        if self.passenger['state'] == 'dormant': #'offline':
            machine = WorkflowStateMachine(start_value=self.passenger['state'])
            s = machine.current_state
            for t in s.transitions:
                if t.destinations[0].name == 'offline': #'offline':
                    data = {
                        "transition": t.identifier,
                        "sim_clock": sim_clock
                    }
                    passenger_item_url = passenger_url + f"/{self.passenger['_id']}"

                    requests.patch(passenger_item_url,
                                    headers=self.user.get_headers(etag=self.passenger['_etag']),
                                    data=json.dumps(data))

                    response = requests.get(passenger_item_url, headers=self.user.get_headers())
                    self.passenger = response.json()

                    return self.login(sim_clock)
        elif self.passenger['state'] == 'offline': #'offline':
            machine = WorkflowStateMachine(start_value=self.passenger['state'])
            s = machine.current_state
            for t in s.transitions:
                if t.destinations[0].name == 'online': #'offline':
                    data = {
                        "transition": t.identifier,
                        "sim_clock": sim_clock
                    }

                    passenger_item_url = passenger_url + f"/{self.passenger['_id']}"

                    requests.patch(passenger_item_url,
                                    headers=self.user.get_headers(etag=self.passenger['_etag']),
                                    data=json.dumps(data))

                    response = requests.get(passenger_item_url, headers=self.user.get_headers())
                    self.passenger = response.json()

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

        requests.patch(item_url,
                        headers=self.user.get_headers(etag=self.passenger['_etag']),
                        data=json.dumps(data))

    def refresh(self):
        passenger_url = f"{settings['OPENRIDE_SERVER_URL']}/{self.run_id}/passenger"
        passenger_item_url = passenger_url + f"/{self.passenger['_id']}"

        response = requests.get(passenger_item_url, headers=self.user.get_headers())

        if is_success(response.status_code):
            self.passenger = response.json()
        else:
            logging.warning(f'PassengerManager.refresh: Failed getting response for {self.passenger["_id"]} Got {response.text}')
