import os, sys
current_path = os.path.abspath('.')
parent_path = os.path.dirname(current_path)
sys.path.append(parent_path)

from mesa import Agent
from .assignment_app import AssignmentApp
from config import settings
from loc_service import PlanningArea

from shapely.geometry import MultiPolygon, mapping


class AssignmentAgent(Agent):
    ''' '''

    def __init__(self, unique_id, model, behavior=None):
        super().__init__(unique_id, model)

        if behavior is not None:
            self.behavior = behavior
        else:
            self.behavior = AssignmentAgent.load_behavior(unique_id)

        self.credentials = {
            'email': self.behavior.get('email'),
            'password': self.behavior.get('password'),
        }

        self.assignment_app = AssignmentApp(model.run_id, model.get_current_time_str(), self.credentials, self.behavior['solver'], self.behavior['solver_params'])

    def get_current_time_str(self):
        return self.model.get_current_time_str()

    @classmethod
    def load_behavior(cls, unique_id, behavior=None):
        ''' '''
        if behavior is None:
            behavior = {
                'email': f'{unique_id}@test.com',
                'password': 'password',

                # 'solver': 'RandomAssignment',
                'solver': 'CompromiseMatching',

                'solver_params': {
                    'name': settings['PLANNING_AREA'],
                    # 'area': {
                    #     # NOTE This must be a MultiPolygon describing the specific region where this engine will gather Supply / demand
                    #     'center': {'type': 'Point', 'coordinates': (103.833057754201, 1.41709038337595)},
                    #     'radius': 50000, # meters
                    # },
                    'area': mapping(PlanningArea().get_planning_area(settings['PLANNING_AREA'])),

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

        # print(f"{behavior['solver_params']['area']=}")
        return behavior


    def step(self):
        ''' '''
        # print('AssignmentAgent.step')
        result = self.assignment_app.assign(self.get_current_time_str(), self.model.service_schedule.time)
        # print('After assign')

        self.assignment_app.publish(result)




