
import os, sys
current_path = os.path.abspath('.')
parent_path = os.path.dirname(current_path)
sys.path.append(parent_path)

import json, math, time

import numpy as np
import pandas as pd
from pymongo import MongoClient
import plotly.express as px
from dateutil.relativedelta import relativedelta

from datetime import date, datetime, tzinfo, timezone
from pymongo.cursor import CursorType

from apps.utils.direct_db_queries import *

data_folder = f'{os.path.dirname(os.path.dirname(os.path.abspath(__file__)))}/output'

def get_all_paths(run_id, num_steps, sim_step_size, reference_time):

    start_hour = reference_time.hour
    end_hour = math.floor(start_hour + (num_steps // (3600//sim_step_size)))

    all_paths = []
    for t in range(start_hour, end_hour, end_hour-start_hour):
        # Generate hourly dataset for paths
        args = {
            'from': f"20200101{t:02}0000",
            'to': f"20200101{t+(end_hour-start_hour):02}0000"
        }

        paths = get_paths(run_id, args, (t-start_hour)*3600)

        # all_paths.append(paths)
        all_paths.extend(paths)

    return all_paths


def get_chart(run_id_dict, metric, kind, title):

    df = get_kpi_time_series(run_id_dict, [metric])
    df['sim_clock'] = df['sim_clock'].dt.time

    if kind == 'cumulative':
        new_df = df[(df['metric'] == metric)][['sim_clock', 'run_id', 'cumulative']]
        new_df.rename(columns={'cumulative': 'value'}, inplace=True)

        # metrics = {
        #     'column_names': ['sim_clock', 'value'],
        #     'graph_data': new_df.round(2).fillna(0).values.tolist()
        # }

        # with open (f"{output_dir}/{m}_cumulative.json", 'w') as file:
        #     json.dump(metrics, file, default=str, indent=2)

    elif kind == 'avg_by_trip':
        new_df = df[(df['metric'] == metric)][['sim_clock', 'run_id', 'avg_by_trip']]
        new_df.rename(columns={'avg_by_trip': 'value'}, inplace=True)
        # new_df['sim_clock'] = new_df.sim_clock.strftime('%H:%M:%S')

        # metrics = {
        #     'column_names': ['sim_clock', 'value'],
        #     'graph_data': new_df.round(2).fillna(0).values.tolist()
        # }

        # with open (f"{output_dir}/{m}_avg_by_trip.json", 'w') as file:
        #     json.dump(metrics, file, default=str, indent=2)


    # Average Metrics by Time
    elif kind == 'avg_by_time':
        new_df = df[(df['metric'] == metric) ][['sim_clock', 'run_id', 'avg_by_time']]
        new_df.rename(columns={'avg_by_time': 'value'}, inplace=True)
        # new_df['target'] = target.get(metric, new_df['value'].max())
        # new_df['sim_clock'] = new_df.sim_clock.strftime('%H:%M:%S')

        # metrics = {
        #     'column_names': ['sim_clock', 'value', 'target'],
        #     'graph_data': new_df.round(2).fillna(0).values.tolist()
        # }

        # with open (f"{output_dir}/{m}_avg_by_time.json", 'w') as file:
        #     json.dump(metrics, file, default=str, indent=2)

    chart_pivot = pd.pivot_table(new_df, index='sim_clock',
                                    columns='run_id', values='value',
                                    aggfunc='first').round(2)

    # chart_pivot = chart_pivot.fillna(0).reset_index()
    chart_pivot = chart_pivot.fillna(0)

    chart = {
        # 'column_names': list(chart_pivot.columns),
        'title': title,
        'type': 'timeseries',
        'column_names': [chart_pivot.index.name] + list(chart_pivot.columns),
        'labels': chart_pivot.index.tolist(),
        'graph_data': chart_pivot.values.tolist()
    }

    return chart

def get_answer_rate(run_id_dict, title):

    df = get_kpi_time_series(run_id_dict, ['num_served', 'num_cancelled'])

    pv = df.pivot_table(index=['run_id', 'sim_clock'], columns=['metric'], values='cumulative')
    pv['value'] = pv['num_served'] / (pv['num_cancelled'] +  pv['num_served'])
    new_df = pv.drop(columns=['num_cancelled', 'num_served']).fillna(0).reset_index()

    new_df['sim_clock'] = new_df['sim_clock'].dt.time

    chart_pivot = pd.pivot_table(new_df, index='sim_clock',
                                    columns='run_id', values='value',
                                    aggfunc='first').round(2)
    chart = {
        # 'column_names': list(chart_pivot.columns),
        'title': title,
        'type': 'timeseries',
        'column_names': [chart_pivot.index.name] + list(chart_pivot.columns),
        'labels': chart_pivot.index.tolist(),
        'graph_data': chart_pivot.values.tolist()
    }

    return chart


def get_billboard(run_id_dict, num_steps, sim_step_size, reference_time):

    active_user_df = get_active_user_time_series(run_id_dict, num_steps, sim_step_size, reference_time)
    active_user_df = pd.pivot_table(active_user_df, index=['run_id', 'sim_clock',], columns='metric', values='value').reset_index()

    billboard_df = pd.DataFrame()

    # add Active user metrics
    billboard_df[['sim_clock']] = active_user_df[['sim_clock']]
    billboard_df['metric'] = 'drivers_vs_passengers'
    billboard_df['value'] = active_user_df['driver_count'].astype(str) + ' vs ' + active_user_df['passenger_count'].astype(str)

    billboard_metrics = [
        'num_served',
        'num_cancelled',
        'revenue',
        'wait_time_pickup',
        'service_score',
    ]

    kpi_df = get_kpi_time_series(run_id_dict, billboard_metrics)

    # answer_rate_by_time
    served_df = kpi_df[(kpi_df['metric'] == 'num_served')][['sim_clock', 'cumulative']].reset_index(drop=True)
    cancelled_df = kpi_df[(kpi_df['metric'] == 'num_cancelled')][['sim_clock', 'cumulative']].reset_index(drop=True)

    new_df = served_df.copy()
    new_df['cumulative'] = served_df['cumulative'] / (served_df['cumulative'] + cancelled_df['cumulative'])
    new_df.rename(columns={'cumulative': 'value'}, inplace=True)
    new_df['metric'] = 'answer_rate'

    billboard_df = billboard_df.append(new_df.round(2).fillna(0))
    billboard_df.reset_index(inplace=True, drop=True)

    # Cumulative metrics
    new_df = kpi_df[(kpi_df['metric'].isin(['num_served', 'revenue']))][['sim_clock', 'metric', 'cumulative']]
    new_df.rename(columns={'cumulative': 'value'}, inplace=True)

    billboard_df = billboard_df.append(new_df.round(2).fillna(0))
    billboard_df.reset_index(inplace=True, drop=True)

    # Average By Time metrics
    new_df = kpi_df[(kpi_df['metric'] == 'wait_time_pickup')][['sim_clock', 'metric', 'avg_by_trip']]
    new_df.rename(columns={'avg_by_trip': 'value'}, inplace=True)

    billboard_df = billboard_df.append(new_df.round(2).fillna(0))
    billboard_df.reset_index(inplace=True, drop=True)


    billboard_pivot = pd.pivot_table(billboard_df, index='sim_clock',
                                    columns='metric', values='value',
                                    aggfunc='first')

    billboard_pivot.reset_index(inplace=True)
    billboard_pivot['sim_clock'] = billboard_pivot['sim_clock'].dt.time

    billboard = {
        'column_names': list(billboard_pivot.columns),
        'graph_data': billboard_pivot.values.tolist()
    }

    return billboard


def get_static_chart(run_id_dict, index, values, aggfunc, title):
    df = get_trip_metrics(run_id_dict)

    chart_pivot = pd.pivot_table(df, index=index,
                                    columns='run_id', values=values,
                                    aggfunc=aggfunc).fillna(0).round(2)# fig = px.line(df2,

    chart = {
        # 'column_names': list(chart_pivot.columns),
        'title': title,
        'type': 'static',
        'column_names': [chart_pivot.index.name] + list(chart_pivot.columns),
        'labels': chart_pivot.index.tolist(),
        'graph_data': chart_pivot.values.tolist()
    }

    return chart

def get_solver_metric_chart(run_id_dict, metric_dict, target = None, title='Update this Title'):

    df = get_engine_perf(run_id_dict)
    df.replace([np.inf, -np.inf], np.nan, inplace=True)
    df.dropna(inplace=True)
    # df['sim_clock'] = df['sim_clock'].dt.time
    df.index = df['sim_clock'].dt.time

    chart_pivot = df[[k for k, _ in metric_dict.items()]].round(2)
    chart_pivot.rename(columns=metric_dict, inplace=True)
    if target is not None:
        chart_pivot['Target'] = target

    chart = {
        # 'column_names': list(chart_pivot.columns),
        'title': title,
        'type': 'timeseries',
        'column_names': [chart_pivot.index.name] + list(chart_pivot.columns),
        'labels': chart_pivot.index.tolist(),
        'graph_data': chart_pivot.values.tolist()
    }

    return chart

# def dump_solver_params(run_id_dict):

#     df = get_engine_perf(run_id_dict)
#     df.replace([np.inf, -np.inf], np.nan, inplace=True)
#     df.dropna(inplace=True)
#     df['sim_clock'] = df['sim_clock'].dt.time
#     # print(df)


#     for run_id, run_id_name in run_id_dict.items():
#         output_dir = f"{data_folder}/{run_id_name}"

#         metric_df = df[(df['run_id'] == run_id)][['sim_clock', 'weight_pickup_time', 'weight_revenue', 'weight_service_score']]
#         metrics = {
#             'column_names': list(metric_df.columns),
#             'graph_data': metric_df.round(2).fillna(0).values.tolist()
#         }
#         # print(metrics)
#         with open (f"{output_dir}/solver_weights.json", 'w') as file:
#             json.dump(metrics, file, default=str, indent=2)


#         metric_df = df[(df['run_id'] == run_id)][['sim_clock', 'pickup_perf', 'revenue_perf', 'service_perf']]
#         metrics = {
#             'column_names': list(metric_df.columns),
#             'graph_data': metric_df.round(2).fillna(0).values.tolist()
#         }
#         with open (f"{output_dir}/solver_objectives.json", 'w') as file:
#             json.dump(metrics, file, default=str, indent=2)

# def dump_trip_metrics(run_id_dict):
#     df = get_trip_metrics(run_id_dict)

#     df.to_csv(f'{data_folder}/trip_metrics.csv', index=False)


if __name__ == '__main__':

    num_steps = 960
    sim_step_size = 30
    reference_time = datetime(2020, 1, 1, 4, 0, 0)

    output_dir = f"{data_folder}/{datetime.strftime(datetime.now(), '%Y%m%d%H%M%S')}"
    print(f"{output_dir = }")
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)

    # primary_run_id = '9YOUfrgBKvdO'
    # primary_run_id = 'juyxDLTucfQj'

    # run_id_dict = {
    #     # 'r0kZnIvJqUWg': 'pickup_optimal', # 'PickupOpt',
    #     # 'sROLL5zudXBx': 'revenue_optimal', # 'RevenueOpt',
    #     # '6OnGpMZ19V0k': 'service_optimal', # 'ServiceOpt',
    #     # # # 'GhWlSdbFp8fD': 'compromise_base', # 'Compromise',
    #     # '9YOUfrgBKvdO': 'compromise_servicebias', # 'CompromiseSvcBias',

    #     '039u1iZAFyI0': 'pickup_optimal',
    #     'd9Ubn85rhnlq': 'revenue_optimal',
    #     'E2MQNKalZ74k': 'service_optimal',
    #     'juyxDLTucfQj': 'compromise_servicebias',


    # } # Comfort Data Set Sampled (10p 06d) Svc Dist 2,

    # target = {
    #     'revenue': 77.5802,
    #     'wait_time_pickup': 2491.5625,
    #     'service_score': 416.38645,
    # }


    # dashboard = {
    #     "title": "Job Design using Gazing Heuristics for overall Platform Efficiency",
    #     'billboard': {
    #         'row': 1,
    #         'column': 1,
    #         'data': get_billboard({primary_run_id: run_id_dict[primary_run_id]}, num_steps, sim_step_size, reference_time),
    #     },
    #     "geo_chart": [
    #         {
    #             'title': 'Realtime Demand',
    #             # 'map': True,
    #             'type': 'density_heatmap',
    #             'row': 2,
    #             'column': 1,
    #             'data': get_demand_coords(primary_run_id, num_steps, sim_step_size, reference_time),
    #         },
    #         {
    #             'title': 'Fulfilment Trips',
    #             # 'map': True,
    #             'type': 'paths',
    #             'row': 2,
    #             'column': 2,
    #             'data': get_all_paths(primary_run_id, num_steps, sim_step_size, reference_time),
    #         },
    #     ],
    #     "chart": [
    #         {
    #             'title': 'Average Waiting Time (Mins)',
    #             # 'map': False,
    #             'type': 'line',
    #             'row': 3,
    #             'column': 1,
    #             'data': get_chart(run_id_dict, 'wait_time_pickup', 'avg_by_time'),
    #         },
    #         {
    #             'title': 'Total revenue ($)',
    #             # 'map': False,
    #             'type': 'bar',
    #             'row': 3,
    #             'column': 2,
    #             'data': get_chart(run_id_dict, 'revenue', 'cumulative'),
    #         },
    #         {
    #             'title': 'Average Service Score',
    #             # 'map': False,
    #             'type': 'line',
    #             'row': 3,
    #             'column': 3,
    #             'data':  get_chart(run_id_dict, 'service_score', 'avg_by_trip'),
    #         },
    #         # {
    #         #     'title': 'Service vs Revenue',
    #         #     'map': False,
    #         #     'type': 'line',
    #         #     'row': 3,
    #         #     'column': 3,
    #         #     'data': {},
    #         # },
    #     ]
    # }

    # with open (f"{output_dir}/dashboard.json", 'w') as file:
    #     json.dump(dashboard, file, default=str, indent=2)

    primary_run_id = 'qO7HWJEAEngT' #'TyJszk5F2yxO'
    primary_run_id_dict = {primary_run_id: "a. Plaform Policy (Gazing)"}
    run_id_dict = primary_run_id_dict.copy()
    run_id_dict.update({
        '3m6YeNAegnji': 'b. Pickup Optimal',
        'HIRxOWvYN1hb': 'c. Revenue Optimal',
        'lQKlTodJMtIh': 'd. Service Optimal',
        # 'TyJszk5F2yxO': 'Online Targeting (Gazing Heuristic)',
    }) # Comfort Data Set Sampled (10p 06d) Svc Dist 2,

    # billboard
    billboard = get_billboard(primary_run_id_dict, num_steps, sim_step_size, reference_time)
    with open (f"{output_dir}/billboard.json", 'w') as file:
        json.dump(billboard, file, default=str, indent=2)

    # demand Coords (map)
    demand_coords = get_demand_coords(primary_run_id, num_steps, sim_step_size, reference_time)
    with open (f"{output_dir}/demand_coords.json", 'w') as file:
        json.dump(demand_coords, file, default=str, indent=2)

    # Paths (map)
    paths = get_all_paths(primary_run_id, num_steps, sim_step_size, reference_time),
    with open (f"{output_dir}/paths.json", 'w') as file:
        json.dump(paths, file, default=str, indent=2)

    # platform revenue (graph (1,1))
    graph_1_1 = get_chart(run_id_dict, 'revenue', 'cumulative', title='Platform Revenue')
    with open (f"{output_dir}/graph_1_1.json", 'w') as file:
        json.dump(graph_1_1, file, default=str, indent=2)

    # Trip Waiting Time (graph (1,2))
    graph_1_2 = get_chart(run_id_dict, 'wait_time_pickup', 'avg_by_trip', title='Customer Waiting Time')
    with open (f"{output_dir}/graph_1_2.json", 'w') as file:
        json.dump(graph_1_2, file, default=str, indent=2)

    # customer Satisfaction (graph (1,3))
    graph_1_3 = get_chart(run_id_dict, 'service_score', 'cumulative', title='Customer Satisfaction Score')
    with open (f"{output_dir}/graph_1_3.json", 'w') as file:
        json.dump(graph_1_3, file, default=str, indent=2)

    # driver revenue (graph (2,1))
    graph_2_1 = get_chart(run_id_dict, 'revenue', 'avg_by_trip', title='Driver Revenue (per Trip)')
    with open (f"{output_dir}/graph_2_1.json", 'w') as file:
        json.dump(graph_2_1, file, default=str, indent=2)

    # # customers Served (graph (2,2))
    # graph_2_2 = get_chart(run_id_dict, 'num_served', 'cumulative', title='Service Rate'),
    # Answer rate (graph (2,2))
    graph_2_2 = get_answer_rate(run_id_dict, title='Customer Service Rate')
    with open (f"{output_dir}/graph_2_2.json", 'w') as file:
        json.dump(graph_2_2, file, default=str, indent=2)


    # # Priority Service (graph (2,3))
    # priority_service_run_id_dict = primary_run_id_dict.copy()
    # priority_service_run_id_dict.update({
    #     'lQKlTodJMtIh': 'Service Optimal',
    # })
    # graph_2_3 = get_static_chart(priority_service_run_id_dict, 'service_score', 'trip_price', 'mean', title='Service based Reward')
    graph_2_3 = get_static_chart(run_id_dict, 'service_score', 'trip_price', 'mean', title='Service based Reward')
    with open (f"{output_dir}/graph_2_3.json", 'w') as file:
        json.dump(graph_2_3, file, default=str, indent=2)


    # Online control parameter (graph (3, 1))
    metric_dict = {
        'weight_pickup_time': 'Waiting (Control)',
        'weight_revenue': 'Revenue (Control)',
        'weight_service_score': 'Service (Control)',
    }
    graph_3_1 = get_solver_metric_chart(primary_run_id_dict, metric_dict, title='Realtime Control (Tuning parameters)')
    with open (f"{output_dir}/graph_3_1.json", 'w') as file:
        json.dump(graph_3_1, file, default=str, indent=2)

    # Online Performance (graph (3, 2))
    metric_dict = {
        'pickup_perf': 'Waiting (Performance)',
        'revenue_perf': 'Revenue (Performance)',
        'service_perf': 'Service (Performance)'
    }
    graph_3_2 = get_solver_metric_chart(primary_run_id_dict, metric_dict, target=100, title='Realtime performance (target=100)')
    with open (f"{output_dir}/graph_3_2.json", 'w') as file:
        json.dump(graph_3_2, file, default=str, indent=2)
