from random import choice
import traceback
from numpy import mat

import logging
from .abstract_solver import AbstractSolver
from pyomo.environ import *

# from apps.config import settings
from apps.config import assignment_settings

class CompromiseMatching(AbstractSolver):
    ''' '''


    def solve(self, driver_list, passenger_trip_list, distance_matrix, online_params):
        '''
        NOTE: Input distance_matrix is Indexed by [driver, passenger]
        The solver needs it in the reversed order i.e [passenger, driver]
        '''

        matched_pairs = self.doMatching(driver_list, passenger_trip_list, distance_matrix, online_params)
        # print(f"{matched_pairs=}")

        if (len(driver_list) > 0) and (len(passenger_trip_list) > 0):
            assignment = [(driver_list[item[1]], passenger_trip_list[item[0]]) for item in matched_pairs]
        else:
            assignment = []

        # print(f"{driver_list=}")
        # print(f"{passenger_trip_list=}")
        # print(f"{assignment=}")

        return assignment, matched_pairs


    def doMatching(self, driverList, paxList, pickupTime, online_params):
        ''' '''

        # print(f"{online_params=}")

        pax_ids = [p['_id'] for p in paxList]
        # driver_ids = [d['_id'] for d in driverList]
        driver_ids = [d['driver'] for d in driverList]
        matchedPairs = []
        # i = 0
        # j = 0


        driverListPotential = []
        driverListPotential_ids = []
        driverPositionMap = []

        # print(f"{pickupTime=}")
        # print(f"{driverList=}")
        # print(f"{paxList=}")

        j=0
        for d in driverList:
            i = 0
            for p in paxList:
                if pickupTime[j][i] < self._params['max_travel_time_pickup']:
                    driverListPotential.append(d)
                    driverListPotential_ids.append(d['driver'])
                    driverPositionMap.append(j) #// j is the position of driver in driverList
                    break
                i += 1
            j += 1

        # print(f"numPax: {len(self.paxList)}, numDrv: {len(driverListPotential)}")

        if len(paxList) > 0 and len(driverListPotential) > 0:
            try:
                compModel = ConcreteModel()

                reducedPickupTime = {}
                c = {}

                compModel.x = Var(pax_ids, driverListPotential_ids, within=NonNegativeReals, bounds=(0,1))

                j = 0
                for d in driverListPotential:
                    i = 0
                    for p in paxList:
                        reducedPickupTime[p['_id'],d['driver']] = pickupTime[driverPositionMap[j]][i] #pickupTime[i][j]
                        if reducedPickupTime[p['_id'],d['driver']] < self._params['max_travel_time_pickup']:
                            c[p['_id'],d['driver']] = (online_params['weight_revenue'] * p['trip_price']) + \
                                                        (online_params['weight_pickup_time'] * (self._params['offline_params']['ub_pickup_time'] - reducedPickupTime[p['_id'],d['driver']]) / 2) + \
                                                        (online_params['weight_service_score'] * d['meta']['profile']['service_score'])
                        else:
                            c[p['_id'],d['driver']] = 0
                            compModel.x[p['_id'],d['driver']].fix(0)
                        i += 1
                    j += 1


                def obj_rule(m):
                    return sum(c[p,d] * m.x[p,d] for p in pax_ids for d in driverListPotential_ids)
                compModel.obj = Objective(rule=obj_rule, sense=-1)

                def atmost_one_pax_per_driver_rule(m, d):
                    return sum(m.x[p,d] for p in pax_ids) <= 1
                if len(pax_ids) > 0:
                    compModel.driver_constr = Constraint(driverListPotential_ids, rule=atmost_one_pax_per_driver_rule)

                # print(driverListPotential_ids)
                def atmost_one_driver_per_pax_rule(m, p):
                    return sum(m.x[p,d] for d in driverListPotential_ids) <= 1
                if len(driverListPotential_ids) > 0:
                    compModel.passenger_constr = Constraint(pax_ids, rule=atmost_one_driver_per_pax_rule)


                opt = SolverFactory('glpk')
                # opt = SolverFactory('gurobi_direct')
                # print('before Solve')
                result = opt.solve(compModel)
                # print('after Solve')

                # compModel.pprint()
                x_i = compModel.x.extract_values()
                # print(x_i)

                # # // Double[][] compSolution = new Double[paxList.size()][driverListPotential.size()];
                i = 0
                for p in paxList:
                    j = 0
                    for d in driverListPotential:
                        if (x_i[p['_id'],d['driver']] > 0.5):
                            newPair = [i, driverPositionMap[j], reducedPickupTime[p['_id'],d['driver']]]
                            matchedPairs.append(newPair)
                            break
                        j += 1
                    i += 1

                # print(f"matched pair size={len(self.matchedPairs)}, tot wait pax={len(self.paxList)}")


            except Exception as e:
                # print(e)
                # print(traceback.format_exc())
                logging.exception(str(e))

                raise e

        return matchedPairs

    def update_online_params(self, time_step, driver_list, passenger_list, matched_pairs, offline_params, online_params):
        ''' '''
        # logging.warning(time_step)
        online_params['exp_target_reverse_pickup_time'] = (time_step + 1) * offline_params['target_reverse_pickup_time']
        online_params['exp_target_revenue'] = (time_step + 1) * offline_params['target_revenue']
        online_params['exp_target_service_score'] = (time_step + 1) * offline_params['target_service_score']

        reverse_pickup_time_step = 0
        revenue_step = 0
        service_score_step = 0

        if matched_pairs is not None:
            if len(matched_pairs) !=0:

                reverse_pickup_time_step = 0
                revenue_step = 0
                service_score_step = 0

                for pair in matched_pairs:

                    driver = driver_list[pair[1]]
                    passenger_trip = passenger_list[pair[0]]
                    matched_pickup_time = pair[2]

                    if matched_pickup_time >= offline_params['ub_pickup_time']:
                        logging.warning(f'{matched_pickup_time = }')

                    reverse_pickup_time_step += ((offline_params['ub_pickup_time'] - matched_pickup_time) / offline_params['scale_factor_reverse_pickup_time']) / 2.5
                    revenue_step += (passenger_trip['trip_price'] / offline_params['scale_factor_revenue'])
                    service_score_step += (driver['meta']['profile']['service_score'] / offline_params['scale_factor_service_score'])


        online_params['realtime_reverse_pickup_time_step'] =  reverse_pickup_time_step
        online_params['realtime_revenue_step'] = revenue_step
        online_params['realtime_service_score_step'] = service_score_step

        online_params['realtime_reverse_pickup_time_cum'] += reverse_pickup_time_step
        online_params['realtime_revenue_cum'] += revenue_step
        online_params['realtime_service_score_cum'] += service_score_step

        online_params['weight_pickup_time'] = max(online_params['exp_target_reverse_pickup_time'] - online_params['realtime_reverse_pickup_time_cum'], 1.0) / (time_step + 1)
        online_params['weight_revenue'] = max(online_params['exp_target_revenue'] - online_params['realtime_revenue_cum'], 1.0) / (time_step + 1)
        online_params['weight_service_score'] = max(online_params['exp_target_service_score'] - online_params['realtime_service_score_cum'], 1.0) / (time_step + 1)

        return online_params
