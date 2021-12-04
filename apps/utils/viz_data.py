
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

pd.options.display.float_format = '{:.2f}'.format


def _connect_mongo(host, port, username, password, db):
    """ A util for making a connection to mongo """

    if username and password:
        mongo_uri = 'mongodb://%s:%s@%s:%s/%s' % (username, password, host, port, db)
        conn = MongoClient(mongo_uri)
    else:
        conn = MongoClient(host, port)


    return conn[db]


def get_paths_from_cursor(collection, args, filter, time_shift=0):
    print(f"get_paths_from_cursor: {args=}")
    # print(args)
    _from = args.get('from')
    from_dt = datetime.strptime(_from, '%Y%m%d%H%M%S').replace(tzinfo=timezone.utc)
    _to = args.get('to')
    to_dt = datetime.strptime(_to, '%Y%m%d%H%M%S').replace(tzinfo=timezone.utc)

    filter["sim_clock"] = {
        "$gte": from_dt,
        "$lt": to_dt
    }
    # print(filter)
    project = {
        '_id': 0,
        "event.state": 1,
        "event.location.coordinates": 1,
        "event.traversed_path": 1,
        "trip": 1,
        "sim_clock": 1,
    }
    sort=list({
        'trip': 1,
        'counter': 1
    }.items())

    cursor = collection.find(
        filter=filter,
        projection=project,
        sort=sort,
        # cursor_type=CursorType.EXHAUST
    )

    trip = {
        'trip_id': None,
        'tripclass': None,
        'path': [],
        'traversed_path': [],
        'timestamps': [],
    }
    paths = []
    print(f"Num Docs: {cursor.count()}")
    proc = 0
    start_time = time.time()
    for document in cursor:
        proc = proc+1
        print(f"Processed: {proc} \r", sep=' ', end='', flush=True)
        trip_id = str(document['trip'])
        coord = [round(x, 5) for x in document['event']['location']['coordinates']]
        traversed_path = document['event'].get('traversed_path') \
                            if document['event'].get('traversed_path') is not None \
                            else []
        tripclass = document['event']['state']
        ts = document['sim_clock'].replace(tzinfo=timezone.utc)

        if trip['trip_id'] is None:
            trip = {
                'trip_id': trip_id,
                'tripclass': tripclass,
                'path': [coord],
                'traversed_path': traversed_path,
                'timestamps': [(ts-from_dt).seconds+time_shift],
            }
        elif (trip['trip_id'] == trip_id) and (trip['tripclass'] == tripclass):
            trip['path'].append(coord)
            trip['traversed_path'].extend(traversed_path)
            trip['timestamps'].append((ts-from_dt).seconds+time_shift)
        else:
            if len(trip['path']) > 1:
                paths.append(trip)
            elif len(trip['path']) == 1:
                # print(trip['path'])
                if trip['tripclass'] in ['passenger_requested_trip']:
                    trip['path'] = trip['path'] + trip['path']
                    trip['traversed_path'] = trip['path']
                    trip['timestamps'].append((ts-from_dt).seconds+time_shift)
                    paths.append(trip)
                elif trip['tripclass'] in ['passenger_cancelled_trip']:
                    trip['path'] = trip['path'] + trip['path']
                    trip['traversed_path'] = trip['path']
                    trip['timestamps'].append(trip['timestamps'][0]+60)
                    paths.append(trip)
                # print(trip['path'])

            trip = {
                'trip_id': trip_id,
                'tripclass': tripclass,
                'path': [coord],
                'traversed_path': traversed_path,
                'timestamps': [(ts-from_dt).seconds+time_shift],
            }

        cur_time = time.time()
        if (cur_time - start_time) > 5:
            print(f"Processed: {proc} in {cur_time - start_time} sec")
            start_time = cur_time

    for trip in paths:
        # print(trip)
        numsteps = len(trip['traversed_path'])
        start = trip['timestamps'][0]
        end = trip['timestamps'][-1]
        # trip['traversed_timsetamps'] = np.linspace(start, end, numsteps).astype('int').tolist()
        traversed_timsetamps = np.linspace(start, end, numsteps).astype('int').tolist()

        trip['path'] = trip.pop('traversed_path')
        trip['timestamps'] = traversed_timsetamps

    # print('Completed path generation')
    return paths

def get_pivot(collection, run_id_meta, metric):
    cursor = collection.find({
            'run_id': {'$in': [k for k, _ in run_id_meta.items()]},
            'metric': metric
        },
        projection={ '_id': 0, 'run_id': 1, 'sim_clock': 1, 'value': 1,},
        sort=[('sim_clock', 1)]
    )

    metric_df = pd.DataFrame(list(cursor))

    metric_pivot = pd.pivot_table(metric_df,
                                  index='sim_clock',
                                  columns='run_id',
                                  values='value').rename(columns=run_id_meta)
    cumulative_pivot = metric_pivot.cumsum()

    metric_pivot_by_time = cumulative_pivot.copy()
    metric_pivot_by_time['time_step'] = list(range(1, len(cumulative_pivot)+1))
    metric_pivot_by_time['value'] = metric_pivot_by_time['value'] / metric_pivot_by_time['time_step']
    metric_pivot_by_time.drop(columns='time_step', inplace=True)

    return metric_pivot_by_time, cumulative_pivot


def count_active_users(collection, run_id_meta, sim_clock_ticks):
    # DRIVER_TRIP = db.driver_ride_hail_trip

    cursor = collection.aggregate([
        {
            '$match': {
                'run_id': {'$in': [k for k, _ in run_id_meta.items()]}
            }
        }, {
            '$group': {
                '_id': {
                    'user': '$user'
                },
                'entered_market': {
                    '$min': '$_created'
                },
                'exit_market': {
                    '$max': '$_updated'
                }
            }
        }
    ])

    docs = list(cursor)
    # results = []
    results = {
        'column_names': ['sim_clock', 'count'],
        'graph_data': []
    }

    for sim_clock in sim_clock_ticks:
        value = 0
        for doc in docs:
            if (doc['entered_market'] <= sim_clock) and (doc['exit_market'] >= sim_clock):
                value += 1

        # results.append({
        #     "sim_clock": sim_clock,
        #     "value": value
        # })
        results['graph_data'].append([sim_clock.strftime('%H:%M:%S'), value])

    return results


def get_engine_perf(collection, run_id_list):
    cursor = collection.find({
            'run_id': {'$in': run_id_list},
        },
        projection={ '_id': 0, 'online_params': 1, 'sim_clock': 1, 'runtime_performance': 1,},
        sort=[('sim_clock', 1)]
    )

    metric_df = pd.DataFrame(list(cursor))
    metric_df = pd.concat([metric_df.drop(['online_params'], axis=1), metric_df['online_params'].apply(pd.Series)], axis=1)
    metric_df = pd.concat([metric_df.drop(['runtime_performance'], axis=1), metric_df['runtime_performance'].apply(pd.Series)], axis=1)


    metric_df['realtime_reverse_pickup_time_cum_rate'] = 100* metric_df['realtime_reverse_pickup_time_cum'] / metric_df['exp_target_reverse_pickup_time']
    metric_df['realtime_revenue_cum_rate'] = 100* metric_df['realtime_revenue_cum'] / metric_df['exp_target_revenue']
    metric_df['realtime_service_score_cum_rate'] = 100* metric_df['realtime_service_score_cum'] / metric_df['exp_target_service_score']

    # print(metric_df.columns)

    return metric_df





def dump(run_id, num_steps, sim_step_size, reference_time, engine_metrics=False):

    db = _connect_mongo(host='localhost', port=27017, username=None, password=None, db='OpenRoadDB')

    KPI = db.kpi
    ENGINE_HISTORY = db.engine_history
    WAYPOINT = db.waypoint
    DRIVER_TRIP = db.driver_ride_hail_trip
    PASSENGER_TRIP = db.passenger_ride_hail_trip

    output_dir = f"{os.path.dirname(os.path.dirname(os.path.abspath(__file__)))}/output/{run_id}"
    print(f"{output_dir = }")
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)

    filter = {
        'run_id': run_id
    }

    start_hour = reference_time.hour
    end_hour = math.floor(start_hour + (num_steps // (3600//sim_step_size)))

    all_paths = []
    for t in range(start_hour, end_hour):
        # Generate hourly dataset for paths
        args = {
            'from': f"20200101{t:02}0000",
            'to': f"20200101{t+1:02}0000"
        }

        paths = get_paths_from_cursor(WAYPOINT, args, filter, (t-start_hour)*3600)

        all_paths.append(paths)

        # with open(f"{output_dir}/paths_{args['from']}_{args['to']}.json", 'w') as file:
        #     json.dump(paths, file, indent=2)

    with open(f"{output_dir}/paths_20200101{start_hour:02}0000_20200101{end_hour:02}0000.json", 'w') as file:
        json.dump(all_paths, file, indent=2)

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
    metric_target_by_time = {
        'revenue': 27.4223229,
        'wait_time_pickup': 731.75,
        'service_score': 124.801,
    }

    run_id_meta = {
        filter['run_id']: 'value',
    }

    pivot_collection = {}
    for m in sum_metric:
        metric_pivot_by_time, cum_pivot = get_pivot(KPI, run_id_meta, m)
        pivot_collection[m] = {
            'metric_pivot': metric_pivot_by_time,
            'cum_pivot': cum_pivot
        }
        cum_pivot['sim_clock'] = metric_pivot_by_time.index.strftime('%H:%M:%S')

        # metrics = cum_pivot.to_dict(orient='records')
        cols = list(cum_pivot.columns)
        cum_pivot = cum_pivot[cols[-1:] + cols[:-1]]
        metrics = {
            'column_names': list(cum_pivot.columns),
            'graph_data': cum_pivot.fillna(0).values.tolist()
        }

        with open (f"{output_dir}/plot_cumulative_{m}.json", 'w') as file:
            json.dump(metrics, file, default=str, indent=2)

    # served_pivot, served_cum_pivot = get_pivot(KPI, run_id_meta, 'num_served')
    served_pivot, served_cum_pivot = pivot_collection['num_served']['metric_pivot'], pivot_collection['num_served']['cum_pivot']
    for m in avg_metric:
        metric_pivot_by_time, cum_pivot = get_pivot(KPI, run_id_meta, m)
        cum_pivot = cum_pivot / served_cum_pivot

        metric_pivot_by_time['target'] = metric_target_by_time[m]
        metric_pivot_by_time['sim_clock'] = metric_pivot_by_time.index.strftime('%H:%M:%S')
        cum_pivot['sim_clock'] = cum_pivot.index.strftime('%H:%M:%S')

        # metrics = cum_pivot.to_dict(orient='records')
        cols = list(cum_pivot.columns)
        if cols[-1] == 'sim_clock':
            cum_pivot = cum_pivot[cols[-1:] + cols[:-1]]
        metrics = {
            'column_names': list(cum_pivot.columns),
            'graph_data': cum_pivot.fillna(0).values.tolist()
        }
        with open (f"{output_dir}/plot_avg_{m}_by_served.json", 'w') as file:
            json.dump(metrics, file, default=str, indent=2)

        # metrics = metric_pivot_by_time.to_dict(orient='records')
        cols = list(metric_pivot_by_time.columns)
        # print(cols)
        metric_pivot_by_time = metric_pivot_by_time[cols[-1:] + cols[:-1]]
        metrics = {
            'column_names': list(metric_pivot_by_time.columns),
            'graph_data': metric_pivot_by_time.fillna(0).values.tolist()
        }
        with open (f"{output_dir}/plot_avg_{m}_by_time.json", 'w') as file:
            json.dump(metrics, file, default=str, indent=2)

        # target_metric_pivot = metric_pivot_by_time.copy()
        # target_metric_pivot['value'] = metric_target_by_time[m]
        # # metrics = target_metric_pivot.to_dict(orient='records')
        # cols = target_metric_pivot.columns
        # target_metric_pivot = target_metric_pivot[cols[-1:] + cols[:-1]]
        # metrics = {
        #     'graph_data': target_metric_pivot.values.tolist()
        # }
        # with open (f"{output_dir}/plot_target_{m}_by_time.json", 'w') as file:
        #     json.dump(metrics, file, default=str, indent=2)

    # Answer Rate
    served_pivot = pivot_collection['num_served']['cum_pivot']
    cancelled_pivot = pivot_collection['num_cancelled']['cum_pivot']
    answer_rate_pivot = served_pivot
    answer_rate_pivot['value'] = served_pivot['value'] / (served_pivot['value'] + cancelled_pivot['value'])
    answer_rate_pivot['sim_clock'] = answer_rate_pivot.index.strftime('%H:%M:%S')

    # metrics = answer_rate_pivot.to_dict(orient='records')
    cols = list(answer_rate_pivot.columns)
    answer_rate_pivot = answer_rate_pivot[cols[-1:] + cols[:-1]]
    metrics = {
        'column_names': list(answer_rate_pivot.columns),
        'graph_data': answer_rate_pivot.fillna(0).values.tolist()
    }

    with open (f"{output_dir}/plot_answer_rate.json", 'w') as file:
        json.dump(metrics, file, default=str, indent=2)

    # num_drivers
    sim_start_time = datetime(2020, 1, 1, reference_time.hour, 0 , 0)
    sim_clock_ticks = [sim_start_time + relativedelta(seconds=(step*sim_step_size)) for step in range(num_steps+1)]
    metrics = count_active_users(DRIVER_TRIP, run_id_meta, sim_clock_ticks)
    with open (f"{output_dir}/num_driver_in_market.json", 'w') as file:
        json.dump(metrics, file, default=str, indent=2)

    # num_passengers
    metrics = count_active_users(PASSENGER_TRIP, run_id_meta, sim_clock_ticks)
    with open (f"{output_dir}/num_passenger_in_market.json", 'w') as file:
        json.dump(metrics, file, default=str, indent=2)


    if engine_metrics:
        # solver_weights
        # pickup_time
        engine_df = get_engine_perf(ENGINE_HISTORY, [k for k, v in run_id_meta.items()])

        # metric_df = engine_df[['sim_clock', 'weight_pickup_time']] \
        #                         .rename(columns={'weight_pickup_time': 'value'})

        # metrics = metric_df.to_dict(orient='records')
        # with open (f"{output_dir}/weight_pickup_obj.json", 'w') as file:
        #     json.dump(metrics, file, default=str, indent=2)

        # # revenue
        # metric_df = engine_df[['sim_clock', 'weight_revenue']] \
        #                         .rename(columns={'weight_revenue': 'value'})

        # metrics = metric_df.to_dict(orient='records')
        # with open (f"{output_dir}/weight_revenue_obj.json", 'w') as file:
        #     json.dump(metrics, file, default=str, indent=2)

        # # Service
        # metric_df = engine_df[['sim_clock', 'weight_service_score']] \
        #                         .rename(columns={'weight_service_score': 'value'})

        # metrics = metric_df.to_dict(orient='records')
        # with open (f"{output_dir}/weight_service_score_obj.json", 'w') as file:
        #     json.dump(metrics, file, default=str, indent=2)
        metric_df = engine_df[['sim_clock', 'weight_pickup_time', 'weight_revenue', 'weight_service_score']]
        metrics = {
            'column_names': list(metric_df.columns),
            'graph_data': metric_df.fillna(0).values.tolist()
        }
        with open (f"{output_dir}/weight_obj.json", 'w') as file:
            json.dump(metrics, file, default=str, indent=2)

        # Objective vs Target
        # pickup_time
        # engine_df = get_engine_perf(ENGINE_HISTORY, [k for k, v in run_id_meta.items()])

        # metric_df = engine_df[['sim_clock', 'realtime_reverse_pickup_time_cum_rate']] \
        #                         .rename(columns={'realtime_reverse_pickup_time_cum_rate': 'value'})

        # metrics = metric_df.to_dict(orient='records')
        # with open (f"{output_dir}/pickup_obj_vs_target.json", 'w') as file:
        #     json.dump(metrics, file, default=str, indent=2)

        # # revenue
        # metric_df = engine_df[['sim_clock', 'realtime_revenue_cum_rate']] \
        #                         .rename(columns={'realtime_revenue_cum_rate': 'value'})

        # metrics = metric_df.to_dict(orient='records')
        # with open (f"{output_dir}/revenue_obj_vs_target.json", 'w') as file:
        #     json.dump(metrics, file, default=str, indent=2)

        # # Service
        # metric_df = engine_df[['sim_clock', 'realtime_service_score_cum_rate']] \
        #                         .rename(columns={'realtime_service_score_cum_rate': 'value'})

        # metrics = metric_df.to_dict(orient='records')
        # with open (f"{output_dir}/service_obj_vs_target.json", 'w') as file:
        #     json.dump(metrics, file, default=str, indent=2)
        metric_df = engine_df[['sim_clock', 'realtime_reverse_pickup_time_cum_rate', 'realtime_revenue_cum_rate', 'realtime_service_score_cum_rate']]
        metrics = {
            'column_names': list(metric_df.columns),
            'graph_data': metric_df.fillna(0).values.tolist()
        }
        with open (f"{output_dir}/obj_vs_target.json", 'w') as file:
            json.dump(metrics, file, default=str, indent=2)


if __name__ == '__main__':
    run_id_list = [
        'r0kZnIvJqUWg',
        'sROLL5zudXBx',
        '6OnGpMZ19V0k',
    ]
    num_steps = 960 # 1920
    step_size = 30
    ref_time = datetime(2016, 12, 1, 4, 0, 0)

    for run_id in run_id_list:
        dump(run_id, num_steps, step_size, ref_time, engine_metrics=False)

    run_id_list = [
        '2gp78vxu3j24',
        'vr7zH3e1OGJc',
        'JLpdE5GzvX1b',
        'RGlocqBN8czZ',
    ]
    for run_id in run_id_list:
        dump(run_id, num_steps, step_size, ref_time, engine_metrics=True)
