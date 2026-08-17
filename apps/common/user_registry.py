import json
from http import HTTPStatus

import requests
from requests.exceptions import ConnectionError

from apps.common.resource_client_mixin import get_http_session
from apps.config import settings
from apps.utils import id_generator, is_success
# from apps.state_machine import WorkflowStateMachine

class UserRegistry:
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
        self.update_user_role()
        if self.token is None:
            raise Exception('Cannot initialize User. Bad Credentials')

    def get_headers(self, etag=None):
        if self.token is None:
            headers = {"Content-Type": "application/json"}
        elif etag is None:
            headers = {"Content-Type": "application/json", "Authorization": f"JWT {self.token['access_token']}"}
        else:
            headers = {"Content-Type": "application/json", "Authorization": f"JWT {self.token['access_token']}", "If-Match": etag}
        return headers

    def user_login(self, sim_clock):
        # Use the shared pooled session — bootstrap alone is ~3 HTTP calls per
        # agent (login attempt + maybe signup + relogin). With 3.6k agents
        # this is ~11k connections; unpooled, they all open + close fresh TCP
        # sockets and dominate cold-start time.
        session = get_http_session()
        login_url = f"{settings['OPENRIDE_SERVER_URL']}/auth/login"
        data = {"email": self.email, "password": self.password, "sim_clock": sim_clock}
        try:
            response = session.post(login_url, headers=self.get_headers(), data=json.dumps(data))
            if is_success(response.status_code):
                return response.json()
            else:
                register_url = f"{settings['OPENRIDE_SERVER_URL']}/auth/signup"
                data = {"email": self.email, "password": self.password, "name": {"first_name": "Dummy", "last_name": "Dummy"}, "public_key": "000", "role": self.role, "sim_clock": sim_clock}
                response = session.post(register_url, headers=self.get_headers(), data=json.dumps(data))
                if is_success(response.status_code):
                    return self.user_login(sim_clock)
                else:
                    return None
        except ConnectionError as e:
            print(f"Unable to connect to OpenRoad Server at {settings['OPENRIDE_SERVER_URL']}. Please ensure the server is running and the URL is correct.")
            raise e

    def update_user_role(self):
        session = get_http_session()
        user_url = f"{settings['OPENRIDE_SERVER_URL']}/user"
        params = {'where': json.dumps({"email": self.email})}
        response = session.get(user_url, headers=self.get_headers(), params=params)
        if is_success(response.status_code):
            user = response.json()['_items'][0]
            if user['role'] != self.role:
                user_item_url = f"{user_url}/{user['_id']}"
                response = session.patch(user_item_url, headers=self.get_headers(etag=user['_etag']), data=json.dumps({"role": self.role}))
                if not is_success(response.status_code):
                    raise Exception(f"Unable to update User Role. Got {response.text}")
