import os, json, time, requests
import geopandas as gpd
# from one_map_auth import OneMapAuth

from shapely.geometry import mapping, shape


class TaxiStop:

    def __init__(self):
        self.dir_path = os.path.dirname(os.path.abspath(__file__))
        self.taxi_stop_file = f"{self.dir_path}/data/lta-taxi-stop/lta-taxi-stop-geojson.geojson"

        self.records = gpd.read_file(self.taxi_stop_file)

        # self.stop_locations = [p for p in self.records.geometry]


    # print(taxi_stops.geometry[0].x, taxi_stops.geometry[0].y, taxi_stops.geometry[0].z)

    # print(OneMapAuth.get_token())

    # def get_locations_within(self, planning_area_name):
    #     planning_area_file = f"{self.dir_path}/data/onemap-planning-area/PlanningArea.json"

    #     with open(planning_area_file) as f:
    #         planning_area_data =  json.load(f)

    #     for area in planning_area_data:
    #         if area["pln_area_n"] == planning_area_name:
    #             defn = shape(json.loads(area['geojson']))
    #             return defn

    #     return None
    def get_locations_within(self, planning_area_name = None):
        planning_area_file = f"{self.dir_path}/data/onemap-planning-area/PlanningArea.json"

        with open(planning_area_file) as f:
            planning_area_data =  json.load(f)

        planning_area_defn = None
        try:
            for area in planning_area_data:
                if area["pln_area_n"] == planning_area_name:
                    planning_area_defn = shape(json.loads(area['geojson']))
        except: pass

        if (planning_area_name is not None) and (planning_area_defn is not None):
            planning_area_records = self.records[self.records.within(planning_area_defn)]
            self.stop_locations = [p for p in planning_area_records.geometry]
        else:
            self.stop_locations = [p for p in self.records.geometry]

        return self.stop_locations


if __name__ == "__main__":
    ts = TaxiStop()

    planning_area = ts.get_locations_within('HOUGANG')

    # print(ts.records[ts.records.within(planning_area)])

    # print(len(ts.records))


    print(ts.stop_locations)
