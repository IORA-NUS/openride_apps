
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

db = _connect_mongo(host='localhost', port=27017, username=None, password=None, db='OpenRoadDB')


def get_trip_metrics(run_id_dict):
    collection = db.driver_ride_hail_trip

    results = collection.aggregate(
        [
            {
                '$match': {
                    'run_id': {'$in': [k for k, v in run_id_dict.items()]},
                    'state': 'driver_completed_trip',
                    'passenger_ride_hail_trip': {
                        '$exists': True
                    }
                }
            }, {
                '$lookup': {
                    'from': 'passenger_ride_hail_trip',
                    'localField': 'passenger_ride_hail_trip',
                    'foreignField': '_id',
                    'as': 'pax'
                }
            }, {
                '$project': {
                    '_id': 0,
                    'run_id': '$run_id',
                    'driver': '$driver',
                    'start_time': '$_created',
                    'service_score': '$meta.profile.service_score',
                    'trip_length': {
                        '$arrayElemAt': [
                            '$pax.last_waypoint.cumulative_stats.distance', 0
                        ]
                    },
                    'trip_price': {
                        '$arrayElemAt': [
                            '$pax.trip_price', 0
                        ]
                    },
                    'wait_time_pickup': {
                        '$arrayElemAt': [
                            '$pax.stats.wait_time_pickup', 0
                        ]
                    },
                    'wait_time_total': {
                        '$arrayElemAt': [
                            '$pax.stats.wait_time_total', 0
                        ]
                    },
                }
            }
        ]
    )

    df = pd.DataFrame(list(results))

    df.run_id.replace(run_id_dict,inplace=True)
    return df

# def get_pivot(collection, run_id_meta, metric):
def get_kpi_time_series(run_id_dict, metric_list):
    collection = db.kpi
    if 'num_served' not in metric_list:
        metric_list.append('num_served')

    cursor = collection.find({
            'run_id': {'$in': [k for k, v in run_id_dict.items()]},
            'metric': {'$in': metric_list}
        },
        projection={ '_id': 0, 'run_id': 1, 'sim_clock': 1, 'metric': 1, 'value': 1,},
        sort=[('run_id', 1), ('metric', 1), ('sim_clock', 1)]
    )

    df = pd.DataFrame(list(cursor))
    df['cumulative'] = 0
    df['avg_by_time'] = 0
    df['avg_by_trip'] = 0


    time_step = None
    for run_id in [k for k, v in run_id_dict.items()]:
        num_served = df[(df['run_id'] == run_id) & (df['metric'] == 'num_served')]['value'].cumsum().tolist()
        for metric in metric_list:
            slice = (df['run_id'] == run_id) & (df['metric'] == metric)
            if time_step is None:
                time_step = list(range(1, len(df[slice])+1))

            df.loc[slice, 'cumulative'] = df[slice]['value'].cumsum()
            df.loc[slice, 'avg_by_time'] = df[slice]['cumulative'] / time_step
            df.loc[slice, 'avg_by_trip'] = df[slice]['cumulative'] / num_served

    df.run_id.replace(run_id_dict,inplace=True)
    return df

def get_active_user_time_series(run_id_dict, num_steps, sim_step_size, sim_start_time):

    sim_clock_ticks = [sim_start_time + relativedelta(seconds=(step*sim_step_size)) for step in range(num_steps+1)]

    query = [
        {
            '$match': {
                'run_id': {'$in': [k for k, v in run_id_dict.items()]}
            }
        }, {
            '$group': {
                '_id': {
                    'user': '$user',
                    'run_id': '$run_id'
                },
                'entered_market': {
                    '$min': '$_created'
                },
                'exit_market': {
                    '$max': '$_updated'
                }
            }
        }
    ]

    driver_collection = db.driver_ride_hail_trip
    driver_cursor = driver_collection.aggregate(query)
    driver_docs = list(driver_cursor)

    pax_collection = db.passenger_ride_hail_trip
    pax_cursor = pax_collection.aggregate(query)
    pax_docs = list(pax_cursor)

    df = pd.DataFrame(columns=['sim_clock', 'run_id', 'metric', 'value']).astype({'value': 'int32'})

    for sim_clock in sim_clock_ticks:
        for run_id in [k for k, v in run_id_dict.items()]:
            d_value = 0
            for doc in driver_docs:
                if (doc['_id']['run_id'] == run_id) and (doc['entered_market'] <= sim_clock) and (doc['exit_market'] >= sim_clock):
                    d_value += 1

            p_value = 0
            for doc in pax_docs:
                if (doc['_id']['run_id'] == run_id) and (doc['entered_market'] <= sim_clock) and (doc['exit_market'] >= sim_clock):
                    p_value += 1

            df = pd.concat([pd.DataFrame([[sim_clock, run_id, 'driver_count', d_value],
                                          [sim_clock, run_id, 'passenger_count', p_value]
                                          ], columns=df.columns),
                            df],ignore_index=True)

    df.run_id.replace(run_id_dict, inplace=True)
    df.sort_values(['run_id', 'metric', 'sim_clock'], inplace=True)

    return df

def get_engine_perf(run_id_dict):
    collection = db.engine_history

    cursor = collection.find({
            'run_id': {'$in': [k for k, v in run_id_dict.items()]},
        },
        projection={ '_id': 0, 'sim_clock': 1, 'run_id': 1, 'online_params': 1,  'runtime_performance': 1,},
        sort=[('sim_clock', 1)]
    )

    metric_df = pd.DataFrame(list(cursor))
    metric_df = pd.concat([metric_df.drop(['online_params'], axis=1), metric_df['online_params'].apply(pd.Series)], axis=1)
    metric_df = pd.concat([metric_df.drop(['runtime_performance'], axis=1), metric_df['runtime_performance'].apply(pd.Series)], axis=1)


    metric_df['pickup_perf'] = 100* metric_df['realtime_reverse_pickup_time_cum'] / metric_df['exp_target_reverse_pickup_time']
    metric_df['revenue_perf'] = 100* metric_df['realtime_revenue_cum'] / metric_df['exp_target_revenue']
    metric_df['service_perf'] = 100* metric_df['realtime_service_score_cum'] / metric_df['exp_target_service_score']

    # print(metric_df.columns)

    return metric_df


def get_paths(run_id, time_args, time_shift=0):
    collection = db.waypoint

    print(f"get_paths_from_cursor: {time_args=}")
    # print(args)
    _from = time_args.get('from')
    from_dt = datetime.strptime(_from, '%Y%m%d%H%M%S').replace(tzinfo=timezone.utc)
    _to = time_args.get('to')
    to_dt = datetime.strptime(_to, '%Y%m%d%H%M%S').replace(tzinfo=timezone.utc)

    filter = {
        "run_id": run_id,
        "sim_clock": {
            "$gte": from_dt,
            "$lt": to_dt
        }
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
            # elif len(trip['path']) == 1:
            #     # print(trip['path'])
            #     if trip['tripclass'] in ['passenger_requested_trip']:
            #         trip['path'] = trip['path'] + trip['path']
            #         trip['traversed_path'] = trip['path']
            #         trip['timestamps'].append((ts-from_dt).seconds+time_shift)
            #         paths.append(trip)
            #     elif trip['tripclass'] in ['passenger_cancelled_trip']:
            #         trip['path'] = trip['path'] + trip['path']
            #         trip['traversed_path'] = trip['path']
            #         trip['timestamps'].append(trip['timestamps'][0]+60)
            #         paths.append(trip)
            #     # print(trip['path'])

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


def get_demand_coords(run_id, num_steps, sim_step_size, sim_start_time):

    sim_clock_ticks = [sim_start_time + relativedelta(seconds=(step*sim_step_size)) for step in range(num_steps+1)]
    collection = db.waypoint

    print(f"get_demand_coords")
    # print(args)
    # _from = time_args.get('from')
    # from_dt = datetime.strptime(_from, '%Y%m%d%H%M%S').replace(tzinfo=timezone.utc)
    # _to = time_args.get('to')
    # to_dt = datetime.strptime(_to, '%Y%m%d%H%M%S').replace(tzinfo=timezone.utc)

    filter = {
        "run_id": run_id,
        "event.state": {"$in": ['passenger_requested_trip', "passenger_pickedup", "passenger_cancelled_trip"]},
        # "sim_clock": {
        #     "$gte": from_dt,
        #     "$lt": to_dt
        # }
    }
    # print(filter)
    project = {
        '_id': 0,
        "event.state": 1,
        "event.location.coordinates": 1,
        # "event.traversed_path": 1,
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

    coord_timeseries = {clock_tick: [] for clock_tick in sim_clock_ticks}
    # print(coord_timeseries)

    proc = 0
    for document in cursor:
        proc = proc+1
        print(f"Processed: {proc} \r", sep=' ', end='', flush=True)
        # trip_id = str(document['trip'])
        # coord = [round(x, 5) for x in document['event']['location']['coordinates']]

        if document['event']['state'] == 'passenger_requested_trip':
            start_ts = document['sim_clock'].replace(tzinfo=None)
            # start_event = document['event']['state']
            start_trip_id = str(document['trip'])
            coord = [round(x, 5) for x in document['event']['location']['coordinates']]
        else:
            end_ts = document['sim_clock'].replace(tzinfo=None)
            end_trip_id = str(document['trip'])
            if start_trip_id == end_trip_id:
                ts = start_ts
                while ts < end_ts:
                    coord_timeseries[ts].append({'COORDINATES': coord})
                    ts += relativedelta(seconds=sim_step_size)


    coordinates = {
        "heatmap_timeseries_data": [
            [datetime.strftime(ts, "%H:%M:%S"), coord_timeseries[ts]] for ts in sim_clock_ticks
        ]
    }

    # print(coord_timeseries)

    # print(coordinates)
    return coordinates
