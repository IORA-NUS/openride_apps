
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

def dump_paths(run_id, run_id_name, num_steps, sim_step_size, reference_time):
    output_dir = f"{data_folder}/{run_id_name}"
    print(f"{output_dir = }")
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)

    # filter = {
    #     'run_id': run_id
    # }

    start_hour = reference_time.hour
    end_hour = math.floor(start_hour + (num_steps // (3600//sim_step_size)))

    all_paths = []
    for t in range(start_hour, end_hour):
        # Generate hourly dataset for paths
        args = {
            'from': f"20200101{t:02}0000",
            'to': f"20200101{t+1:02}0000"
        }

        paths = get_paths(run_id, args, (t-start_hour)*3600)

        all_paths.append(paths)

    with open(f"{output_dir}/paths_20200101{start_hour:02}0000_20200101{end_hour:02}0000.json", 'w') as file:
        json.dump(all_paths, file, indent=2)

def dump_demand_coords(run_id, run_id_name, num_steps, sim_step_size, reference_time):
    output_dir = f"{data_folder}/{run_id_name}"
    print(f"{output_dir = }")
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)

    coords = get_demand_coords(run_id, num_steps, sim_step_size, reference_time)

    with open(f"{output_dir}/demand_coords.json", 'w') as file:
        json.dump(coords, file, indent=2)


def dump_kpi_metrics(run_id_dict, target):

    sum_metric = [
        'num_served',
        'num_cancelled',
        'revenue',
        'wait_time_pickup',
        'service_score'
    ]
    avg_metric = [
        'revenue',
        'wait_time_pickup',
        'service_score',
    ]

    df = get_kpi_time_series(run_id_dict, sum_metric)
    df['sim_clock'] = df['sim_clock'].dt.time

    df.to_csv(f'{data_folder}/kpi_time_series.csv', index=False)


    for run_id, run_id_name in run_id_dict.items():
        output_dir = f"{data_folder}/{run_id_name}"
        # print(f"{output_dir = }")
        if not os.path.exists(output_dir):
            os.makedirs(output_dir)

        # Cumulative Metrics
        for m in sum_metric:
            new_df = df[(df['metric'] == m) & (df['run_id'] == run_id_name)][['sim_clock', 'cumulative']]
            new_df.rename(columns={'cumulative': 'value'}, inplace=True)

            metrics = {
                'column_names': ['sim_clock', 'value'],
                'graph_data': new_df.round(2).fillna(0).values.tolist()
            }

            with open (f"{output_dir}/{m}_cumulative.json", 'w') as file:
                json.dump(metrics, file, default=str, indent=2)

        # Average Metrics by Trip
        for m in avg_metric:
            new_df = df[(df['metric'] == m) & (df['run_id'] == run_id_name)][['sim_clock', 'avg_by_trip']]
            new_df.rename(columns={'avg_by_trip': 'value'}, inplace=True)
            # new_df['sim_clock'] = new_df.sim_clock.strftime('%H:%M:%S')

            metrics = {
                'column_names': ['sim_clock', 'value'],
                'graph_data': new_df.round(2).fillna(0).values.tolist()
            }

            with open (f"{output_dir}/{m}_avg_by_trip.json", 'w') as file:
                json.dump(metrics, file, default=str, indent=2)


        # Average Metrics by Time
        for m in avg_metric:
            new_df = df[(df['metric'] == m) & (df['run_id'] == run_id_name)][['sim_clock', 'avg_by_time']]
            new_df.rename(columns={'avg_by_time': 'value'}, inplace=True)
            new_df['target'] = target.get(m, new_df['value'].max())
            # new_df['sim_clock'] = new_df.sim_clock.strftime('%H:%M:%S')

            metrics = {
                'column_names': ['sim_clock', 'value', 'target'],
                'graph_data': new_df.round(2).fillna(0).values.tolist()
            }

            with open (f"{output_dir}/{m}_avg_by_time.json", 'w') as file:
                json.dump(metrics, file, default=str, indent=2)

        # Answer Rate by time
        served_df = df[(df['metric'] == 'num_served') & (df['run_id'] == run_id_name)][['sim_clock', 'cumulative']].reset_index(drop=True)
        cancelled_df = df[(df['metric'] == 'num_cancelled') & (df['run_id'] == run_id_name)][['sim_clock', 'cumulative']].reset_index(drop=True)

        new_df = served_df.copy()
        new_df['cumulative'] = served_df['cumulative'] / (served_df['cumulative'] + cancelled_df['cumulative'])
        new_df.rename(columns={'cumulative': 'value'}, inplace=True)

        metrics = {
            'column_names': ['sim_clock', 'value'],
            'graph_data': new_df.round(2).fillna(0).values.tolist()
        }

        with open (f"{output_dir}/answer_rate.json", 'w') as file:
            json.dump(metrics, file, default=str, indent=2)


def dump_active_agents(run_id_dict, num_steps, sim_step_size, reference_time):

    df = get_active_user_time_series(run_id_dict, num_steps, sim_step_size, reference_time)
    df['sim_clock'] = df['sim_clock'].dt.time

    df.to_csv(f'{data_folder}/active_users_time_series.csv', index=False)

    metric_list = [
        'driver_count',
        'passenger_count',
    ]

    for run_id, run_id_name in run_id_dict.items():
        output_dir = f"{data_folder}/{run_id_name}"
        # print(f"{output_dir = }")
        if not os.path.exists(output_dir):
            os.makedirs(output_dir)

        # Counts
        for m in metric_list:
            new_df = df[(df['metric'] == m) & (df['run_id'] == run_id_name)][['sim_clock', 'value']]

            metrics = {
                'column_names': ['sim_clock', 'value'],
                'graph_data': new_df.round(2).fillna(0).values.tolist()
            }

            with open (f"{output_dir}/{m}_by_time.json", 'w') as file:
                json.dump(metrics, file, default=str, indent=2)


def dump_solver_params(run_id_dict):

    df = get_engine_perf(run_id_dict)
    df.replace([np.inf, -np.inf], np.nan, inplace=True)
    df.dropna(inplace=True)
    df['sim_clock'] = df['sim_clock'].dt.time


    for run_id, run_id_name in run_id_dict.items():
        output_dir = f"{data_folder}/{run_id_name}"

        metric_df = df[(df['run_id'] == run_id_name)][['sim_clock', 'weight_pickup_time', 'weight_revenue', 'weight_service_score']]
        metrics = {
            'column_names': list(metric_df.columns),
            'graph_data': metric_df.round(2).fillna(0).values.tolist()
        }
        with open (f"{output_dir}/solver_weights.json", 'w') as file:
            json.dump(metrics, file, default=str, indent=2)


        metric_df = df[(df['run_id'] == run_id_name)][['sim_clock', 'pickup_perf', 'revenue_perf', 'service_perf']]
        metrics = {
            'column_names': list(metric_df.columns),
            'graph_data': metric_df.round(2).fillna(0).values.tolist()
        }
        with open (f"{output_dir}/solver_objectives.json", 'w') as file:
            json.dump(metrics, file, default=str, indent=2)


def dump_trip_metrics(run_id_dict):
    df = get_trip_metrics(run_id_dict)

    df.to_csv(f'{data_folder}/trip_metrics.csv', index=False)



if __name__ == '__main__':

    run_id_dict = {
        'r0kZnIvJqUWg': 'pickup_optimal', # 'PickupOpt',
        'sROLL5zudXBx': 'revenue_optimal', # 'RevenueOpt',
        '6OnGpMZ19V0k': 'service_optimal', # 'ServiceOpt',
        'GhWlSdbFp8fD': 'compromise_base', # 'Compromise',
        '9YOUfrgBKvdO': 'compromise_servicebias', # 'CompromiseSvcBias',
    } # Comfort Data Set Sampled (10p 06d) Svc Dist 2,

    target = {
        'revenue': 27.4223229,
        'wait_time_pickup': 731.75,
        'service_score': 124.801,
    }


    for run_id, name in run_id_dict.items():
        dump_paths(run_id, name, 960, 30, datetime(2020, 1, 1, 4, 0, 0))
        dump_demand_coords(run_id, name, 960, 30, datetime(2020, 1, 1, 4, 0, 0))

    dump_kpi_metrics(run_id_dict, target)

    dump_active_agents(run_id_dict, 960, 30, datetime(2020, 1, 1, 4, 0, 0))

    dump_solver_params(run_id_dict)

    dump_trip_metrics(run_id_dict)

