import requests, json
from http import HTTPStatus

import logging
from apps.config import settings
from apps.utils import id_generator, is_success
from apps.lib import WorkflowStateMachine


class EngineManager():

    def __init__(self, run_id, sim_clock, user, solver):
        self.run_id = run_id
        self.user = user
        self.solver = solver

        try:
            self.engine = self.init_engine(sim_clock)
        except Exception as e:
            logging.exception(str(e))
            # print(e)

    def as_dict(self):
        return self.engine

    def get_id(self):
        return self.engine['_id']


    def init_engine(self, sim_clock):
        engine_url = f"{settings['OPENRIDE_SERVER_URL']}/{self.run_id}/engine"

        params = {
            'where': json.dumps({
                'name': self.solver.params['planning_area']['name']
            })
        }
        response = requests.get(engine_url, headers=self.user.get_headers(), params=params)
        # print(response.text)

        if response.json()['_meta']['total'] == 0:
            # Need to register and actvate engine
            response = self.create_engine(sim_clock)
            return self.init_engine(sim_clock)

        else:
            return response.json()['_items'][0]


    def create_engine(self, sim_clock):
        engine_url = f"{settings['OPENRIDE_SERVER_URL']}/{self.run_id}/engine"
        # print(f"{self.solver.__class__.__name__=}")
        data = {
            'name': self.solver.params['planning_area']['name'],
            'strategy': self.solver.__class__.__name__,
            # 'area': '',
            'planning_area': self.solver.params['planning_area'],
            'offline_params': self.solver.params['offline_params'],
            'online_params': self.solver.params['online_params'],
        }

        response =  requests.post(engine_url, headers=self.user.get_headers(), data=json.dumps(data))
        # print(response.text)
        return response

    def update_engine(self, sim_clock, online_params, performance):

        engine_url = f"{settings['OPENRIDE_SERVER_URL']}/{self.run_id}/engine/{self.engine['_id']}"

        data = {
            "online_params": online_params,
            "last_run_performance": performance,
            "sim_clock": sim_clock,
        }
        response = requests.patch(engine_url, headers=self.user.get_headers(etag=self.engine['_etag']), data=json.dumps(data))
        # print(response.text)


    def refresh(self):
        engine_url = f"{settings['OPENRIDE_SERVER_URL']}/{self.run_id}/engine"
        engine_item_url = engine_url + f"/{self.engine['_id']}"

        response = requests.get(engine_item_url, headers=self.user.get_headers())

        self.engine = response.json()
