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
                # pickupTime[i][j] = (abs(d.currentLat - p.originLat) + abs(d.currentLon - p.originLon)) * Params.distanceTimeConversion
                # if True: #pickupTime[i][j] < Params.maxAllowedPickupTime:
                if pickupTime[j][i] < self._params['max_travel_time_pickup']:
                    driverListPotential.append(d)
                    # driverListPotential_ids.append(d['_id'])
                    driverListPotential_ids.append(d['driver'])
                    driverPositionMap.append(j) #// j is the position of driver in driverList
                    break
                i += 1
            j += 1

        # print(f"numPax: {len(self.paxList)}, numDrv: {len(driverListPotential)}")

        if len(paxList) > 0 and len(driverListPotential) > 0:
            try:
                # compModel = gp.Model("Compromise Matching")
                compModel = ConcreteModel()
                # reducedPickupTime = [[0 for d in driverListPotential] for p in self.paxList]
                # c = [[0 for d in driverListPotential] for p in self.paxList]
                reducedPickupTime = {}
                c = {}

                j = 0
                for d in driverListPotential:
                    i = 0
                    for p in paxList:
                        # reducedPickupTime[p.id,d.id] = ((abs(d.currentLat - p.originLat) + abs(d.currentLon - p.originLon)) * Params.distanceTimeConversion)
                        # print(f"{i=}, {j=}")
                        # reducedPickupTime[p['_id'],d['_id']] = pickupTime[j][i] #pickupTime[i][j]
                        reducedPickupTime[p['_id'],d['driver']] = pickupTime[j][i] #pickupTime[i][j]
                        # if True: #reducedPickupTime[p.id,d.id] < Params.maxAllowedPickupTime:
                        if reducedPickupTime[p['_id'],d['driver']] < self._params['max_travel_time_pickup']:
                            # NOTE use [driver_profile]
                            # c[p['_id'],d['_id']] = (online_params['weightRevenue'] * p['trip_price']) + (online_params['weightPickupTime'] * (self.params['offline_params']['reverseParameter'] - reducedPickupTime[p['_id'],d['_id']])/2) + (online_params['weightServiceScore'] * d['settings']['service_score'])
                            c[p['_id'],d['driver']] = (online_params['weightRevenue'] * p['trip_price']) + (online_params['weightPickupTime'] * (self.params['offline_params']['reverseParameter'] - reducedPickupTime[p['_id'],d['driver']])/2) + (online_params['weightServiceScore'] * d['meta']['profile']['service_score'])
                        else:
                            # c[p['_id'],d['_id']] = 0
                            c[p['_id'],d['driver']] = 0
                        i += 1
                    j += 1

                compModel.x = Var(pax_ids, driverListPotential_ids, within=NonNegativeReals, bounds=(0,1))

                # x = [[None for d in driverListPotential] for p in self.paxList]

                # j = 0
                # for d in driverListPotential:
                #     i = 0
                #     for p in self.paxList:
                #         reducedPickupTime[i][j] = ((abs(d.currentLat - p.originLat) + abs(d.currentLon - p.originLon)) * Params.distanceTimeConversion)
                #         if reducedPickupTime[i][j] < Params.maxAllowedPickupTime:
                #             # print(f'reducedPickupTime:{i},{j}: {reducedPickupTime[i][j]}')
                #             x[i][j] = compModel.addVar(0, 1.0, (Params.weightRevenue * p.revenue) + (Params.weightPickupTime * (Params.reverseParameter - reducedPickupTime[i][j])/2) + (Params.weightServiceScore * d.serviceScore), GRB.BINARY, name=f"x_{i}_{j}")
                #             # print(x[i][j], (Params.weightRevenue * p.revenue) + (Params.weightPickupTime * (Params.reverseParameter - reducedPickupTime[i][j])/2) + (Params.weightServiceScore * d.serviceScore))
                #         else:
                #             x[i][j] = compModel.addVar(0, 0.0, 1.0, GRB.BINARY, f"x_{i}_{j}")
                #         i += 1
                #     j += 1

                # print(f"Wait Pax={len(self.paxList)}, Idle Driver={len(self.driverList)}, Potential Driver={len(driverListPotential)}")

                # compModel.setAttr('ModelSense', -1)
                # compModel.update()
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

                # for i in range(len(self.paxList)):
                #     # GRBLinExpr expr_link = new GRBLinExpr()
                #     expr_link = gp.LinExpr()
                #     for j in range(len(driverListPotential)):
                #         expr_link.add(x[i][j], 1)
                #     compModel.addConstr(expr_link, GRB.LESS_EQUAL, 1.0, f"expr_link_{i}")

                # for j in range(len(driverListPotential)):
                #     expr_link_2 = gp.LinExpr()
                #     for i in range(len(self.paxList)):
                #         expr_link_2.add(x[i][j], 1)
                #     compModel.addConstr(expr_link_2, GRB.LESS_EQUAL, 1.0, f"expr_link_2_{j}")

                # // Solve
                # compModel.optimize()
                # compModel.write('p1.lp')

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
                        # if (x_i[p['_id'],d['_id']] > 0.5):
                        #     newPair = [i, driverPositionMap[j], reducedPickupTime[p['_id'],d['_id']]]
                        #     matchedPairs.append(newPair)
                        #     break
                        if (x_i[p['_id'],d['driver']] > 0.5):
                            newPair = [i, driverPositionMap[j], reducedPickupTime[p['_id'],d['driver']]]
                            matchedPairs.append(newPair)
                            break
                        j += 1
                    i += 1

                # print(f"matched pair size={len(self.matchedPairs)}, tot wait pax={len(self.paxList)}")

                # compModel.dispose()
                # env.dispose()
                # gp.disposeDefaultEnv()
                # return matchedPairs

            except Exception as e:
                # print(e)
                # print(traceback.format_exc())
                logging.exception(str(e))

                raise e

        return matchedPairs

    def update_online_params(self, clock_tick, STEPS_PER_ACTION, driver_list, passenger_list, matched_pairs, offline_params, online_params):
        ''' '''
        if matched_pairs is not None:
            if len(matched_pairs) !=0:
                for pair in matched_pairs:

                    driver = driver_list[pair[1]]
                    passenger_trip = passenger_list[pair[0]]
                    matchedPickupTime = pair[2]

                    online_params['realtimePickupTime'] = online_params['realtimePickupTime'] + (offline_params['reverseParameter'] - matchedPickupTime)/offline_params['reverseParameter2']
                    online_params['realtimeRevenue'] = online_params['realtimeRevenue'] + passenger_trip['trip_price']
                    # NOTE: use driver_profile instead of settings
                    # online_params['realtimeServiceScore'] = online_params['realtimeServiceScore'] + driver['settings']['service_score']
                    online_params['realtimeServiceScore'] = online_params['realtimeServiceScore'] + driver['meta']['profile']['service_score']

                # end for
                # online_params['weightPickupTime'] = max((clock_tick / assignment_settings['STEPS_PER_ACTION'] + 1) * offline_params['targetReversePickupTime'] - online_params['realtimePickupTime'], 1.0) / (clock_tick / assignment_settings['STEPS_PER_ACTION'] + 1)
                # online_params['weightRevenue'] = max((clock_tick / assignment_settings['STEPS_PER_ACTION'] + 1) * offline_params['targetRevenue'] - online_params['realtimeRevenue'], 1.0) / (clock_tick / assignment_settings['STEPS_PER_ACTION'] + 1)
                # online_params['weightServiceScore'] = max((clock_tick / assignment_settings['STEPS_PER_ACTION'] + 1) * offline_params['targetServiceScore'] - online_params['realtimeServiceScore'], 1.0) / (clock_tick / assignment_settings['STEPS_PER_ACTION'] + 1)
                online_params['weightPickupTime'] = max((clock_tick / STEPS_PER_ACTION + 1) * offline_params['targetReversePickupTime'] - online_params['realtimePickupTime'], 1.0) / (clock_tick / STEPS_PER_ACTION + 1)
                online_params['weightRevenue'] = max((clock_tick / STEPS_PER_ACTION + 1) * offline_params['targetRevenue'] - online_params['realtimeRevenue'], 1.0) / (clock_tick / STEPS_PER_ACTION + 1)
                online_params['weightServiceScore'] = max((clock_tick / STEPS_PER_ACTION + 1) * offline_params['targetServiceScore'] - online_params['realtimeServiceScore'], 1.0) / (clock_tick / STEPS_PER_ACTION + 1)

        return online_params
