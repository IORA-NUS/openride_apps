
import os, sys, json

from dateutil.relativedelta import relativedelta
current_path = os.path.abspath('.')
parent_path = os.path.dirname(current_path)
sys.path.append(parent_path)

from datetime import datetime
from apps.utils.create_dashboard import get_all_paths, get_demand_coords
from apps.utils.direct_db_queries import paths_to_geojson, coords_to_df

data_folder = f'{os.path.dirname(os.path.dirname(os.path.abspath(__file__)))}/output'

if __name__ == '__main__':

    num_steps = 960
    sim_step_size = 30
    reference_time = datetime(2020, 1, 1, 4, 0, 0)
    # primary_run_id = '9YOUfrgBKvdO'
    # primary_run_id = 'juyxDLTucfQj'
    # primary_run_id = 'qO7HWJEAEngT'
    primary_run_id = 'Oq2TzyY3HnnR'

    paths = get_all_paths(primary_run_id, num_steps, sim_step_size, reference_time)
    geojson = paths_to_geojson(paths, reference_time)

    coords = get_demand_coords(primary_run_id, num_steps, sim_step_size, reference_time, retval='raw')
    df = coords_to_df(coords)



    # print(geojson['features'][0])
    output_dir = f"{data_folder}/{datetime.strftime(datetime.now(), '%Y%m%d%H%M%S')}"
    print(f"{output_dir = }")
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)

    with open(f"{output_dir}/routes.geojson", 'w') as fp:
        json.dump(geojson, fp, indent=2)

    df.to_csv(f"{output_dir}/demand.csv", index=False)
