import os, json, time, requests
import geopandas as gpd
import pandas as pd
# from one_map_auth import OneMapAuth

from shapely.geometry import mapping, shape, Point
from .planning_area import PlanningArea


class BusStop:

    def __init__(self):
        self.dir_path = os.path.dirname(os.path.abspath(__file__))
        self.bus_stop_file = f"{self.dir_path}/data/lta-bus-stops/BusStops.json"

        if not os.path.exists(self.bus_stop_file):
            BusStop.retrieve_bus_stops_from_lta_datamall()

        with open(self.bus_stop_file) as json_file:
            df = pd.DataFrame(json.load(json_file))

        self.records = gpd.GeoDataFrame(df, geometry=gpd.points_from_xy(df.Longitude, df.Latitude))
        # self.stop_locations = [p for p in self.records.geometry]


    def retrieve_bus_stops_from_lta_datamall(cls):
        ''' '''
        dir_path = os.path.dirname(os.path.abspath(__file__))
        bus_stop_file = f"{dir_path}/data/lta-bus-stops/BusStops.json"

        with open(f"{dir_path}/.lta_datamall_accountkey", 'r') as f:
            lta_accountkey = f.readline().rstrip()

        url = "http://datamall2.mytransport.sg/ltaodataservice/BusStops"
        headers = {
            "Accountkey": lta_accountkey,
            "accept": "application/json",
        }
        stop = False
        # record_count = 0
        busstop_list = []
        while not stop:
            ''' '''
            response = requests.request("GET", url=f"{url}?$skip={len(busstop_list)}", headers=headers)

            # print(response, len(busstop_list))
            busstop_response = response.json()
            if len(busstop_response.get('value')) > 0:
                busstop_list.extend(busstop_response.get('value'))
                # print (busstop_list)
                # break
            else:
                stop = True

        with open(bus_stop_file, 'w') as json_file:
            ''' '''
            json.dump(busstop_list, json_file)

    # def get_stop_locations(self):

    #     with open(self.bus_stop_file) as json_file:
    #         df = pd.DataFrame(json.load(json_file))
    #     # self.records = pd.DataFrame(self.bus_stop_json.get('value'))

    #     self.records = gpd.GeoDataFrame(df, geometry=gpd.points_from_xy(df.Longitude, df.Latitude))


    #     # self.records['location'] =  self.records.apply(lambda r: Point(r['Longitude'], r['Latitude']), axis=1)
    #     # # print(self.records)
    #     # self.stop_locations = tuple(zip(self.records.Latitude, self.records.Longitude))

    #     # self.stop_locations = self.records['location'].to_list()
    #     # # self.stop_locations = [p for p in self.records['location']]

    #     self.stop_locations = [p for p in self.records.geometry]

    #     return self.stop_locations


    # def get_locations_within(self, pln_area_n = None):
    def get_locations_within(self, pln_area_list = None):

        # planning_area_defn = PlanningArea().get_planning_area_geometry(pln_area_n)

        # if (pln_area_n is not None) and (planning_area_defn is not None):
        #     planning_area_records = self.records[self.records.within(planning_area_defn)]
        #     self.stop_locations = [p for p in planning_area_records.geometry]
        # else:
        #     self.stop_locations = [p for p in self.records.geometry]

        self.stop_locations = []

        if pln_area_list is None:
            self.stop_locations = [p for p in self.records.geometry]
        else:
            # print(pln_area_list)
            # for pln_area_n in pln_area_list:
            #     print(pln_area_n)
            #     planning_area_defn = PlanningArea().get_planning_area_geometry(pln_area_n)
                # if (pln_area_n is not None) and (planning_area_defn is not None):
                #     planning_area_records = self.records[self.records.within(planning_area_defn)]
                #     self.stop_locations.extend([p for p in planning_area_records.geometry])

            planning_area_defn = PlanningArea().get_planning_area_geometry(pln_area_list)
            if planning_area_defn is not None:
                planning_area_records = self.records[self.records.within(planning_area_defn)]
                self.stop_locations.extend([p for p in planning_area_records.geometry])


        return self.stop_locations


if __name__ == "__main__":
    bs = BusStop()
    bs.get_locations_within(['CLEMENTI'])
    # bs.get_stop_locations()

    print(bs.stop_locations)
    print(len(bs.stop_locations))

    # planning_area = bs.get_locations_within('HOUGANG')

    # print(bs.records[bs.records.within(planning_area)])

    # print(len(bs.records))


    # print(ts.records)
