import requests, json
from http import HTTPStatus

from apps.config import settings
from apps.utils import id_generator, is_success
from apps.state_machine import WorkflowStateMachine

# from utils.registration import Registration
from apps.utils.user_registry import UserRegistry

class PassengerManager():

    # token = None
    # entity = None

    def __init__(self, run_id, sim_clock, user, passenger_settings):
        # super().__init__(sim_clock, credentials)
        self.run_id = run_id
        self.user = user

        self.passenger_settings = passenger_settings

        # self.entity_type = 'passenger'
        self.passenger = self.init_passenger(sim_clock)
        # self.passenger = self.init_passenger()

    def as_dict(self):
        return self.passenger

    def get_id(self):
        return self.passenger['_id']

    def init_passenger(self, sim_clock):
        passenger_url = f"{settings['OPENRIDE_SERVER_URL']}/{self.run_id}/passenger"

        response = requests.get(passenger_url, headers=self.user.get_headers())

        if response.json()['_meta']['total'] == 0:
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
            # "settings": {
            #     "patience": 180
            # },
            "sim_clock": sim_clock
        }

        return requests.post(create_passenger_url, headers=self.user.get_headers(), data=json.dumps(data))

    def login(self, sim_clock):
        passenger_url = f"{settings['OPENRIDE_SERVER_URL']}/{self.run_id}/passenger"

        # print(passenger)
        if self.passenger['state'] == 'dormant': #'offline':
            machine = WorkflowStateMachine(start_value=self.passenger['state'])
            s = machine.current_state
            for t in s.transitions:
                if t.destinations[0].name == 'offline': #'offline':
                    data = {
                        "transition": t.identifier,
                        "sim_clock": sim_clock
                    }
                    # print(data)
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
                    # print(data)
                    passenger_item_url = passenger_url + f"/{self.passenger['_id']}"

                    requests.patch(passenger_item_url,
                                    headers=self.user.get_headers(etag=self.passenger['_etag']),
                                    data=json.dumps(data))

                    response = requests.get(passenger_item_url, headers=self.user.get_headers())
                    self.passenger = response.json()

                    return self.login(sim_clock)
        elif self.passenger['state'] == 'online': #'offline':
            # return response.json()['_items'][0]
            return None
        else:
            raise Exception ("unknown Workflow State")

    def logout(self, sim_clock):
        ''' '''
        # passenger_item_url = settings['OPENRIDE_SERVER_URL'] + f'/passenger/{self.passenger["_id"]}'
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

        self.passenger = response.json()
