import requests, json, logging, traceback
from http import HTTPStatus

from apps.config import settings
from apps.utils import id_generator, is_success
from apps.state_machine import WorkflowStateMachine


class DriverManager():

    def __init__(self, run_id, sim_clock, user, profile):
        self.run_id = run_id
        self.user = user
        self.profile = profile

        try:
            self.driver = self.init_driver(sim_clock)
        except Exception as e:
            logging.exception(str(e))
            # print(e)

        self.vehicle = self.init_vehicle(sim_clock)

    def as_dict(self):
        return self.driver

    def get_id(self):
        return self.driver['_id']


    def init_driver(self, sim_clock):
        ''' Get / Create the driver record '''

        driver_url = f"{settings['OPENRIDE_SERVER_URL']}/{self.run_id}/driver"

        response = requests.get(driver_url, headers=self.user.get_headers())
        # print(response.json())

        if len(response.json()['_items']) == 0:
            # Need to register and actvate driver
            response = self.create_driver(sim_clock)
            return self.init_driver(sim_clock)
        else:
            driver = response.json()['_items'][0]

        return driver


    def create_driver(self, sim_clock):
        create_driver_url = f"{settings['OPENRIDE_SERVER_URL']}/{self.run_id}/driver"

        data = {
            "license": {
                "num": id_generator(), # NOt Guarunteed to be Unique. So need to take care of possible exceptions
                "country": "Singapore",
                "expiry":  "Tue, 01 Jan 2030 00:00:00 GMT"
            },
            "profile": self.profile,
            "sim_clock": sim_clock,
        }

        return requests.post(create_driver_url, headers=self.user.get_headers(), data=json.dumps(data))


    def login(self, sim_clock):
        ''' Assuming that init_driver is already called (i.e. self.driver exists)
            - Ensures that driver is logged in
            - NOTE: driver must be logged in before creating a trip (empty or occupied)
        NOTE: this method can be simplified by leveraging on the statemachine
        '''
        driver_url = f"{settings['OPENRIDE_SERVER_URL']}/{self.run_id}/driver"

        # print(self.driver)

        if self.driver['state'] == 'dormant':
            machine = WorkflowStateMachine(start_value=self.driver['state'])
            s = machine.current_state
            for t in s.transitions:
                if t.destinations[0].name == 'offline':
                    # print(t)
                    data = {
                        "transition": t.identifier,
                        "sim_clock": sim_clock
                    }
                    # print(data)
                    driver_item_url = driver_url + f"/{self.driver['_id']}"

                    response = requests.patch(driver_item_url,
                                headers=self.user.get_headers(etag=self.driver['_etag']),
                                data=json.dumps(data))
                    # print(response.json())
                    response = requests.get(driver_item_url, headers=self.user.get_headers())
                    self.driver = response.json()

                    return self.login(sim_clock)
        elif self.driver['state'] == 'offline':
            machine = WorkflowStateMachine(start_value=self.driver['state'])
            s = machine.current_state
            for t in s.transitions:
                if t.destinations[0].name == 'online':
                    # print(t)
                    data = {
                        "transition": t.identifier,
                        "sim_clock": sim_clock
                    }
                    # print(data)
                    driver_item_url = driver_url + f"/{self.driver['_id']}"

                    response = requests.patch(driver_item_url,
                                                headers=self.user.get_headers(self.driver['_etag']),
                                                data=json.dumps(data))
                    # print(response.json())
                    response = requests.get(driver_item_url, headers=self.user.get_headers())
                    self.driver = response.json()

                    return self.login(sim_clock)
        elif self.driver['state'] == 'online':

            return None
        else:
            raise Exception ("unknown Workflow State")

    def logout(self, sim_clock):
        ''' Ensure proper logout procedure is followed.
        - Once logged out, Driver will be unable to create / update trips
        '''
        item_url = f"{settings['OPENRIDE_SERVER_URL']}/{self.run_id}/driver/{self.get_id()}"

        data = {
            "transition": "logout",
            "sim_clock": sim_clock,
        }

        response = requests.patch(item_url,
                        headers=self.user.get_headers(etag=self.driver['_etag']),
                        data=json.dumps(data))

        if is_success(response.status_code):
            self.refresh()
        else:
            raise Exception(response.text)


    def init_vehicle(self, sim_clock):
        vehicle_url = f"{settings['OPENRIDE_SERVER_URL']}/{self.run_id}/vehicle"

        response = requests.get(vehicle_url, headers=self.user.get_headers())

        if len(response.json()['_items']) == 0:
            # Need to register and actvate vehicle
            response = self.create_vehicle(sim_clock)
            return self.init_vehicle(sim_clock)

        else:
            vehicle = response.json()['_items'][0]

        # print(driver)
        if vehicle['state'] == 'offline':
            return response.json()['_items'][0]
        else:
            machine = WorkflowStateMachine(start_value=vehicle['state'])
            s = machine.current_state
            for t in s.transitions:
                if t.destinations[0].name == 'offline':
                    data = {
                        "transition": t.identifier,
                        "sim_clock": sim_clock,
                    }
                    # print(data)
                    vehicle_item_url = vehicle_url + f"/{vehicle['_id']}"

                    requests.patch(vehicle_item_url,
                                    headers=self.user.get_headers(vehicle['_etag']),
                                    data=json.dumps(data))
                    return self.init_vehicle(sim_clock)

    def create_vehicle(self, sim_clock):
        vehicle_url = f"{settings['OPENRIDE_SERVER_URL']}/{self.run_id}/vehicle"

        data = {
            "registration": {
                "num": id_generator(6), # NOT Guarunteed to be Unique. So need to take care of possible exceptions
                "country": "Singapore",
                "expiry":  "Tue, 01 Jan 2030 00:00:00 GMT"
            },
        	"capacity": 4,
            "sim_clock": sim_clock,
        }

        response =  requests.post(vehicle_url, headers=self.user.get_headers(), data=json.dumps(data))
        # print(response.text)
        return response

    def refresh(self):
        driver_item_url = f"{settings['OPENRIDE_SERVER_URL']}/{self.run_id}/driver/{self.driver['_id']}"

        response = requests.get(driver_item_url, headers=self.user.get_headers())

        if is_success(response.status_code):
            self.driver = response.json()
        else:
            raise Exception(f'DriverManager.refresh: Failed getting response for {self.driver["_id"]} Got {response.text}')
