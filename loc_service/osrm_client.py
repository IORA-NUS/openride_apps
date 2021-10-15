import os, json, time, requests
from random import sample

from shapely.geometry import Point, mapping
import polyline

from config import settings

# from one_map_auth import OneMapAuth

class OSRMClient:

    profile = "driving"
    # def __init__(self):
    #     self.start_loc = None
    #     self.end_loc = None

    #     self.route = None

    @classmethod
    def get_route(cls, start, end):
        '''
        start, end: Geojson geometry dict for Point
        return: list of tuples representing a route
        '''

        # start_latlon = f"{start.y},{start.x}"
        # end_latlon = f"{end.y},{end.x}"
        start_lon_lat = f"{start['coordinates'][0]},{start['coordinates'][1]}"
        end_lon_lat = f"{end['coordinates'][0]},{end['coordinates'][1]}"

        url = f"{settings['ROUTING_SERVER']}/route/v1/{cls.profile}/{start_lon_lat};{end_lon_lat}" # ?geometries=geojson
        # url = f"http://localhost:5000/route/v1/{route_type}/{start_lon_lat};{end_lon_lat}?geometries=geojson"

        params = {
            "overview": "full",
            "alternatives": "false",
            "steps": "false",
            # "hints": "false"
        }
        try:
            response = requests.get(url, params=params)
            # print(response.url)
        except Exception as e:
            # print(e)
            raise(e)

        route_description = response.json()
        # # print(route_description)

        # # return route_description.get('route_geometry')
        # route = route_description['routes'][0]['geometry']

        # # return polyline.decode(route.rstrip())
        # return route['coordinates']
        return route_description['routes'][0]

    @classmethod
    def get_coords_from_route(cls, route):
        route_coords = polyline.decode(route['geometry'].rstrip())
        route_coords = [(x[1], x[0]) for x in route_coords]

        return route_coords

    @classmethod
    def get_distance_matrix(cls, supply_locs, demand_locs, units='duration'):
        '''
        NOTE: Ensure no bugs due to Dict-List conversions.
        '''
        base_url = f"{settings['ROUTING_SERVER']}/table/v1/{cls.profile}"

        supply_fmt_list = [f"{v['coordinates'][0]},{v['coordinates'][1]}" for _, v in  supply_locs.items()]
        demand_fmt_list = [f"{v['coordinates'][0]},{v['coordinates'][1]}" for _, v in  demand_locs.items()]

        if (len(supply_fmt_list) > 0) and (len(supply_fmt_list) > 0):
            all_coords = ';'.join(supply_fmt_list) + ";" + ';'.join(demand_fmt_list)
            source_indices = list(range(len(supply_fmt_list)))
            destination_indices = list(range(len(supply_fmt_list), len(supply_fmt_list) + len(demand_fmt_list)))

            table_url = f"{base_url}/{all_coords}?sources={';'.join([str(i) for i in source_indices])}&destinations={';'.join([str(i) for i in destination_indices])}&annotations={units}&fallback_speed=40.0"

            # print(table_url)
            response = requests.get(table_url)
            # print(response.text)

            try:
                return response.json()[f"{units}s"] # NOTE Plural durations
            except: return None
        # else:
        #     print("Failed to get_distance_matrix: ", supply_fmt_list, demand_fmt_list)

        return None

if __name__== '__main__':

    start = mapping(Point(103.9358139038086, 1.3680111770191412))
    end = mapping(Point(103.79402160644531, 1.3422691724221012))

    route = OSRMClient.get_route(start, end)

    print(route)
