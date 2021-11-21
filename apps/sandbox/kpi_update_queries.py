import pandas as pd
from pymongo import MongoClient
import plotly.express as px
from datetime import datetime
from dateutil.relativedelta import relativedelta

def _connect_mongo(host, port, username, password, db):
    """ A util for making a connection to mongo """

    if username and password:
        mongo_uri = 'mongodb://%s:%s@%s:%s/%s' % (username, password, host, port, db)
        conn = MongoClient(mongo_uri)
    else:
        conn = MongoClient(host, port)


    return conn[db]

db = _connect_mongo(host='localhost', port=27017, username=None, password=None, db='OpenRoadDB')

KPI = db.kpi
ENGINE = db.engine_history

run_id_meta = {
    '8X6m0Rkz5G1W': 'Greedy 1',
    'cyHmxRlQD0Yq': 'Greedy 2',
    'sCsrXR4Kecz9': 'Random 1',
    'FIQgwybgpIv6': 'Random 2',
#     '': 'Compromise',
}




def update_service_score(db, run_id):

    WAYPOINT = db.waypoint
    DRIVER_TRIP = db.driver_ride_hail_trip
    KPI = db.kpi


    cursor = WAYPOINT.find({
            'run_id': run_id,
            'event.state': 'driver_accepted_trip'
        },
        projection={'sim_clock': 1, 'trip': 1,},
        sort=[('sim_clock', 1)]
    )

    wp_df = pd.DataFrame(list(cursor))

# #     wp_pivot = pd.pivot_table(wp_df, index=['sim_clock', 'trip'], values='_id', aggfunc='count')
#     wp_group = wp_df.groupby(['sim_clock', 'trip']).size().reset_index(name='counts')

    for d in range(480):
        clock = datetime(2020, 1, 1, 8, 0, 0) + relativedelta(seconds=d*30)
        trips = list(wp_df[wp_df['sim_clock'] == clock]['trip'])

        cursor = DRIVER_TRIP.find({
                'run_id': run_id,
                '_id': {'$in': trips}
            },
            projection={'_id': 0, 'meta.profile.service_score': 1},
        )
        scores = (list(cursor))
        step_score = 0
        for item in scores:
            step_score += item['meta']['profile']['service_score']

        KPI.update_one({
            'run_id': run_id,
            'metric': 'service_score',
            'sim_clock': clock
        },
        {
            '$set': {
                'run_id': run_id,
                'metric': 'service_score',
                'value': step_score,
                'sim_clock': clock,
                '_updated': datetime.now(),
                '_created': datetime.now(),
                '_etag': 'dummy',
            }
        }, True)
#         break

def update_num_accepted(db, run_id):

    WAYPOINT = db.waypoint
    DRIVER_TRIP = db.driver_ride_hail_trip
    KPI = db.kpi


    cursor = WAYPOINT.find({
            'run_id': run_id,
            'event.state': 'driver_accepted_trip'
        },
        projection={'sim_clock': 1, 'trip': 1,},
        sort=[('sim_clock', 1)]
    )

    wp_df = pd.DataFrame(list(cursor))

# #     wp_pivot = pd.pivot_table(wp_df, index=['sim_clock', 'trip'], values='_id', aggfunc='count')
#     wp_group = wp_df.groupby(['sim_clock', 'trip']).size().reset_index(name='counts')

    for d in range(480):
        clock = datetime(2020, 1, 1, 8, 0, 0) + relativedelta(seconds=d*30)
        trips = list(wp_df[wp_df['sim_clock'] == clock]['trip'])

        cursor = DRIVER_TRIP.find({
                'run_id': run_id,
                '_id': {'$in': trips}
            },
            projection={'_id': 0, 'meta.profile.service_score': 1},
        )
        scores = (list(cursor))
#         step_score = 0
#         for item in scores:
#             step_score += item['meta']['profile']['service_score']
        num_accepted = len(scores)

        KPI.update_one({
            'run_id': run_id,
            'metric': 'num_accepted',
            'sim_clock': clock
        },
        {
            '$set': {
                'run_id': run_id,
                'metric': 'num_accepted',
                'value': num_accepted,
                'sim_clock': clock,
                '_updated': datetime.now(),
                '_created': datetime.now(),
                '_etag': 'dummy',
            }
        }, True)
#         break

for run_id, v in run_id_meta.items():
    update_service_score(db, run_id)
    update_num_accepted(db, run_id)


# DELETE metric Query
# result =  KPI.delete_many({'metric': 'service_score'})
