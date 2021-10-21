import os, json, time, requests
# import geopandas as gpd
import pandas as pd
# from one_map_auth import OneMapAuth
from shapely.geometry import mapping, shape, Point


class PlanningArea:

    def __init__(self):
        self.dir_path = os.path.dirname(os.path.abspath(__file__))
        self.planning_area_file = f"{self.dir_path}/data/onemap-planning-area/PlanningArea.json"

        self.records = None

        # self.refresh_onemap_token()

    def refresh_onemap_token(self):
        ''''''
        url = 'https://developers.onemap.sg/privateapi/auth/post/getToken'

        headers = {
            "accept": "application/json",
            "Content-Type": "application/json",
        }

        with open(f"{self.dir_path}/.onemap_credentials", 'r') as f:
            data = json.load(f)

        response = requests.post(url=url, headers=headers, data=data)

        token = response.json()

        # print(token)
        with open(f"{self.dir_path}/.onemap_token", 'w') as f:
            json.dump(token, f)


    def retrieve_planning_area_from_onemap(self):
        ''' '''
        with open(f"{self.dir_path}/.onemap_token", 'r') as f:
            onemap_token = json.load(f)

        url = "https://developers.onemap.sg/privateapi/popapi/getAllPlanningarea"
        headers = {
            "accept": "application/json",
        }
        params = {
            "token": onemap_token['access_token'],
            "year": 2014 # Constant. This is latest date
        }

        response = requests.get(url=url, headers=headers, params=params)

        planning_area = response.json()
        # print(planning_area)

        with open(self.planning_area_file, 'w') as json_file:
            ''' '''
            json.dump(planning_area, json_file)

    def get_planning_area(self, pln_area_n):

        with open(self.planning_area_file) as json_file:
            # planning_area = pd.DataFrame(json.load(json_file))
            planning_area = json.load(json_file)

        # print(planning_area)
        # return planning_area
        planning_area_defn = None
        try:
            for area in planning_area:
                # print(area)
                if area["pln_area_n"] == pln_area_n:
                    planning_area_defn = shape(json.loads(area['geojson']))
                    return planning_area_defn
        except Exception as e:
            raise e
            # return None


if __name__ == '__main__':

    planning_area = PlanningArea()

    planning_area.retrieve_planning_area_from_onemap()

