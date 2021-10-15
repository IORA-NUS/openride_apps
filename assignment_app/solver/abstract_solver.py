from abc import ABC, abstractmethod


class AbstractSolver(ABC):
    ''' '''
    params = None

    def __init__(self, params):
        ''' '''
        self._params = params

    # def get_params(self):
    #     return self.params

    @property
    def params(self):
        return self._params

    @abstractmethod
    def solve(self, driver_list, passenger_trip_list, distance_matrix, online_params={}):
        pass

    @abstractmethod
    def update_online_params(self, clock_tick, driver_list, passenger_list, matched_pairs, offline_params, online_params):
        pass
