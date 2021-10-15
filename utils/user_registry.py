import requests, json
from http import HTTPStatus

from config import settings
from utils import id_generator, is_success
from lib import WorkflowStateMachine

# from abc import ABC, abstractmethod


class UserRegistry():

    token = None
    entity_type = ''
    entity = {}
    sim_clock = None

    def __init__(self, sim_clock, credentials, role='client'):
        self.email = credentials['email']
        self.password = credentials['password']
        self.sim_clock = sim_clock
        self.role = role

        self.token = self.user_login(sim_clock)

        # check if user role matches, if not update user role
        self.update_user_role()


        if self.token is None:
            raise Exception('Cannot initialize User. Bad Credentials')

    # def get_id(self):
    #     return self.entity['_id']

    def get_headers(self, etag=None):
        ''' This Authentication and header generation should be a shared service '''
        if self.token is None:
            headers = {
                "Content-Type": "application/json",
            }
        elif etag is None:
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"JWT {self.token['access_token']}",
            }
        else:
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"JWT {self.token['access_token']}",
                "If-Match": etag,
            }

        return headers


    def user_login(self, sim_clock):
        login_url = f"{settings['OPENRIDE_SERVER_URL']}/auth/login"
        # headers = {
        #     "Content-Type": "application/json"
        # }
        data = {
            "email": self.email,
            "password": self.password,
            "sim_clock": sim_clock,
        }

        response = requests.post(login_url, headers=self.get_headers(), data=json.dumps(data))
        # print(response.json())

        if is_success(response.status_code):
            # Login is successful
            return response.json()
        else:
            register_url = f"{settings['OPENRIDE_SERVER_URL']}/auth/signup"
            data = {
                "email": self.email,
                "password": self.password,
                "name": {
                    "first_name": "Dummy",
                    "last_name": "Dummy"
                },
                "public_key": "000",
                "role": self.role,
                "sim_clock": sim_clock,
            }

            response = requests.post(register_url, headers=self.get_headers(), data=json.dumps(data))
            if is_success(response.status_code):
                return self.user_login(sim_clock)
            else:
                return None

    def update_user_role(self):
        user_url = f"{settings['OPENRIDE_SERVER_URL']}/user"
        params = {
            'where': json.dumps({"email": self.email})
        }
        response = requests.get(user_url, headers=self.get_headers(), params=params)
        # print( response.url)
        user = response.json()['_items'][0]
        # print(user)
        if user['role'] != self.role:
            user_item_url = f"{user_url}/{user['_id']}"
            response = requests.patch(user_item_url,
                                    headers=self.get_headers(etag=user['_etag']),
                                    data=json.dumps({"role": self.role}))

            if not is_success(response.status_code):
                raise Exception(f"Unable to update User Role. Got {response.text}")


    # def logout(self, sim_clock):
    #     ''' '''
    #     # passenger_item_url = settings['OPENRIDE_SERVER_URL'] + f'/passenger/{self.passenger["_id"]}'
    #     item_url = settings['OPENRIDE_SERVER_URL'] + f'/{self.entity_type}/{self.entity["_id"]}'

    #     data = {
    #         "transition": "logout",
    #         "sim_clock": sim_clock,
    #     }

    #     headers = self.get_headers()
    #     headers['If-Match'] = self.entity['_etag']

    #     requests.patch(item_url, headers=headers, data=json.dumps(data))

    # @abstractmethod
    # def refresh(self):
    #     pass
