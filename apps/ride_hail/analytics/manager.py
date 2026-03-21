# import requests, json
# from http import HTTPStatus

# import logging
# from apps.config import settings, simulation_domains
# from apps.utils import id_generator, is_success
# # from apps.agent_core.state_machine.workflow_sm import WorkflowStateMachine


# from apps.agent_core.base_manager import BaseManager
# from apps.common.resource_client_mixin import ResourceClientMixin

# class AnalyticsManager(ResourceClientMixin, BaseManager):


#     def __init__(self, run_id, sim_clock, user, persona):
#         self.run_id = run_id
#         self.user = user
#         self.persona = persona
#         # self.resource_type = 'kpi'
#         self.simulation_domain = simulation_domains['ridehail']

#         # params = {
#         #     'where': json.dumps({
#         #         'name': self.solver.params['planning_area']['name']
#         #     })
#         # }

#         # data = {
#         #     'name': self.solver.params['planning_area']['name'],
#         #     'strategy': self.solver.__class__.__name__,
#         #     'planning_area': self.solver.params['planning_area'],
#         #     'offline_params': self.solver.params['offline_params'],
#         #     'online_params': self.solver.params['online_params'],
#         # }
#         self.resource = self.init_resource(sim_clock, data=data, params={})


#     def login(self, sim_clock):
#         """
#         AnalyticsManager does not require login. This is a no-op for interface compatibility.
#         """
#         pass

#     def logout(self, sim_clock):
#         """
#         AnalyticsManager does not require logout. This is a no-op for interface compatibility.
#         """
#         pass


#     # create_analytics is now handled by BaseManager's create_resource

#     # def update_analytics(self, sim_clock, online_params, performance):
#     #     data = {
#     #         "online_params": online_params,
#     #         "last_run_performance": performance,
#     #         "sim_clock": sim_clock,
#     #     }
#     #     result = self.resource_patch(resource_id=self.resource['_id'], data=data, etag=self.resource.get('_etag'))
#     #     if not result:
#     #         logging.warning(f"Update Analytics Failed")



