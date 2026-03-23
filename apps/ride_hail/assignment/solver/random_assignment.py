from random import choice

from .abstract_solver import AbstractSolver

class RandomAssignment(AbstractSolver):
    ''' '''

    def solve(self, driver_list, passenger_list, distance_matrix, offline_params, online_params={}):
        ''' '''
        assignment = []

        for passenger_item in passenger_list:
            if len(driver_list) > 0:
                driver_item = choice(driver_list)
                driver_list.remove(driver_item)

                assignment.append((driver_item, passenger_item))
            else:
                break

        return assignment, []


    def update_online_params(self, scale_factor, driver_list, passenger_list, matched_pairs, offline_params, online_params):
        return online_params
