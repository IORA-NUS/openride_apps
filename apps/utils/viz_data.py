import json, os, math

import pandas as pd
from pymongo import MongoClient
import plotly.express as px
from dateutil.relativedelta import relativedelta

from datetime import date, datetime, tzinfo, timezone
from pymongo.cursor import CursorType



def _connect_mongo(host, port, username, password, db):
    """ A util for making a connection to mongo """

    if username and password:
        mongo_uri = 'mongodb://%s:%s@%s:%s/%s' % (username, password, host, port, db)
        conn = MongoClient(mongo_uri)
    else:
        conn = MongoClient(host, port)


    return conn[db]


def get_paths_from_cursor(collection, args, filter):

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
        cursor_type=CursorType.EXHAUST
    )

    trip = {
        'trip_id': None,
        'tripclass': None,
        'path': [],
        'traversed_path': [],
        'timestamps': [],
    }
    paths = []
    for document in cursor:
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
                'timestamps': [(ts-from_dt).seconds],
            }
        elif (trip['trip_id'] == trip_id) and (trip['tripclass'] == tripclass):
            trip['path'].append(coord)
            trip['traversed_path'].extend(traversed_path)
            trip['timestamps'].append((ts-from_dt).seconds)
        else:
            if len(trip['path']) > 1:
                paths.append(trip)
            trip = {
                'trip_id': trip_id,
                'tripclass': tripclass,
                'path': [coord],
                'traversed_path': traversed_path,
                'timestamps': [(ts-from_dt).seconds],
            }

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

    return metric_pivot, cumulative_pivot



def dump(run_id, num_steps, sim_step_size):

    db = _connect_mongo(host='localhost', port=27017, username=None, password=None, db='OpenRoadDB')

    KPI = db.kpi
    ENGINE = db.engine_history
    WAYPOINT = db.waypoint

    output_dir = f"{os.path.dirname(os.path.dirname(os.path.abspath(__file__)))}/output/{run_id}"
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)

    filter = {
        'run_id': run_id
    }

    start_hour = 8
    end_hour = math.floor(start_hour + (num_steps // (3600//sim_step_size)))

    for t in range(start_hour, end_hour):
        # Generate hourly dataset for paths
        args = {
            'from': f"20200101{t:02}0000",
            'to': f"20200101{t+1:02}0000"
        }

        paths = get_paths_from_cursor(WAYPOINT, args, filter)

        with open(f"{output_dir}/paths_{args['from']}_{args['to']}.json", 'w') as file:
            json.dump(paths, file, indent=2)


    sum_metric = [
        'served',
        'cancelled',
        'revenue',
        'wait_time_pickup',
        'service_score'
    ]
    avg_metric_byServed = [
        'revenue',
        'wait_time_pickup',
        'service_score',
    ]

    run_id_meta = {
        filter['run_id']: 'value',
    }
    for m in sum_metric:
        metric_pivot, cum_pivot = get_pivot(KPI, run_id_meta, m)
        cum_pivot['sim_clock'] = metric_pivot.index

        metrics = cum_pivot.to_dict(orient='records')

        with open (f"{output_dir}/plot_cumulative_{m}.json", 'w') as file:
            json.dump(metrics, file, default=str, indent=2)

    served_pivot, served_cum_pivot = get_pivot(KPI, run_id_meta, 'served')
    for m in avg_metric_byServed:
        metric_pivot, cum_pivot = get_pivot(KPI, run_id_meta, m)
        metric_pivot = metric_pivot / served_pivot
        cum_pivot = cum_pivot / served_cum_pivot

        metric_pivot['sim_clock'] = metric_pivot.index
        cum_pivot['sim_clock'] = cum_pivot.index

        metrics = cum_pivot.to_dict(orient='records')

        with open (f"{output_dir}/plot_avg_{m}_by_served.json", 'w') as file:
            json.dump(metrics, file, default=str, indent=2)



