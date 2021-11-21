import os, sys
current_path = os.path.abspath('.')
parent_path = os.path.dirname(current_path)
sys.path.append(parent_path)

from random import choice, random
import traceback, pprint
from numpy import mat

import logging
from .abstract_solver import AbstractSolver
# from pyomo.environ import *

# from apps.config import settings
from apps.config import assignment_settings

class GreedyMinPickupMatching(AbstractSolver):
    ''' '''

    def solve(self, driver_list, passenger_trip_list, distance_matrix, online_params):
        '''
        NOTE: Input distance_matrix is Indexed by [driver, passenger]
        The solver needs it in the reversed order i.e [passenger, driver]
        '''
        assignment = []
        assigned_passenger_idx = [False] * len(passenger_trip_list)

        for i in range(len(driver_list)):
            nearest_idx = -1
            # nearest_dist = 9999999999
            nearest_dist = self._params['max_travel_time_pickup'] + 1
            for j in range(len(passenger_trip_list)):
                # get nearest passenger
                if not assigned_passenger_idx[j]:
                    if distance_matrix[i][j] < nearest_dist:
                        nearest_dist = distance_matrix[i][j]
                        nearest_idx = j

            if nearest_idx != -1:
                assignment.append((driver_list[i], passenger_trip_list[nearest_idx]))
                assigned_passenger_idx[nearest_idx] = True

        # print(f"{driver_list=}")
        # print(f"{passenger_trip_list=}")
        # print(f"{assignment=}")

        return assignment, []



    def update_online_params(self, time_step, driver_list, passenger_list, matched_pairs, offline_params, online_params):
        ''' '''
        return online_params



if __name__ == '__main__':

    d_list = list(range(10))
    p_list = list(range(10))

    dist_matrix = [[random()
                    for j in range(len(p_list)) ]
                        for i in range(len(d_list)) ]
    pprint.pprint(dist_matrix)

    assignment, matched_pairs = GreedyMinPickupMatching(None).solve(d_list, p_list, dist_matrix, None)
    pprint.pprint(assignment)
