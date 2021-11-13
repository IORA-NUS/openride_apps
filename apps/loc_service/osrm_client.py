import os, json, time, requests
from random import sample

from shapely.geometry import Point, mapping
import polyline

from apps.config import settings

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

        url = f"{settings['ROUTING_SERVER']}/route/v1/{cls.profile}/{start_lon_lat};{end_lon_lat}"

        params = {
            "overview": "full",
            "alternatives": "false",
            "steps": "true",
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



from geopy.distance import Distance

from shapely.geometry import LineString, Point
from math import atan2,degrees


def cut(line, distance):
    '''inputs:
    line: in lat-lon Degrees
    distance: in meters
    '''
    # Cuts a line in two at a distance from its starting point
    if type(line) == Point:
        return [line]

    line = LineString(line)
    # convert distance from meters to degrees. Note approximation only valid near Equator
    # https://www.usna.edu/Users/oceano/pguth/md_help/html/approx_equivalents.htm
    distance = distance / 111000
    if distance == 0:
        return [LineString(line)]
    if distance < 0.0:
        raise Exception(f"Nonnegative {distance=}")
    elif distance >= line.length:
        coords = list(line.coords)
        return [Point(coords[-1])]

    coords = list(line.coords)
    for i, p in enumerate(coords):
        pd = line.project(Point(p))
        if pd == distance:
            return [LineString(coords[:i+1]),
                    LineString(coords[i:])]
        if pd > distance:
            cp = line.interpolate(distance)
            return [LineString(coords[:i] + [(cp.x, cp.y)]),
                    LineString([(cp.x, cp.y)] + coords[i:])]



def get_angle(p1, p2):
    return degrees(atan2(p2[1]-p1[1], p2[0]-p1[0]))


from pyproj import Transformer

TRAN_4326_TO_3857 = Transformer.from_crs("EPSG:4326", "EPSG:3857")

def transform_lonlat_webmercator(lon, lat):
  return TRAN_4326_TO_3857.transform(lon, lat)

def itransform_lonlat_webmercator(lonlat_points):
#   return TRAN_4326_TO_3857.transform(lon, lat)
  return TRAN_4326_TO_3857.itransform(lonlat_points)



if __name__== '__main__':

    start = mapping(Point(103.9358139038086, 1.3680111770191412))
    end = mapping(Point(103.79402160644531, 1.3422691724221012))

    route = OSRMClient.get_route(start, end)

    print(route)
