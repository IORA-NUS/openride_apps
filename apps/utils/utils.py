import string
import random
import collections

# from geopy.distance import Distance

# from shapely.geometry import LineString, Point
# from math import atan2,degrees


def id_generator(size=12, chars=string.ascii_lowercase + string.ascii_uppercase + string.digits):
    return ''.join(random.SystemRandom().choice(chars) for _ in range(size))


def is_success(status_code):
    return (status_code >= 200) and (status_code <= 299)


def deep_update(source, overrides):
    """
    Update a nested dictionary or similar mapping.
    Modify ``source`` in place.
    """
    for key, value in overrides.iteritems():
        if isinstance(value, collections.Mapping) and value:
            returned = deep_update(source.get(key, {}), value)
            source[key] = returned
        else:
            source[key] = overrides[key]
    return source



# def cut(line, distance):
#     '''inputs:
#     line: in lat-lon Degrees
#     distance: in meters
#     '''
#     # Cuts a line in two at a distance from its starting point
#     if type(line) == Point:
#         return [line]

#     line = LineString(line)
#     # convert distance from meters to degrees. Note approximation only valid near Equator
#     # https://www.usna.edu/Users/oceano/pguth/md_help/html/approx_equivalents.htm
#     distance = distance / 111000
#     if distance == 0:
#         return [LineString(line)]
#     if distance < 0.0:
#         raise Exception(f"Nonnegative {distance=}")
#     elif distance >= line.length:
#         coords = list(line.coords)
#         return [Point(coords[-1])]

#     coords = list(line.coords)
#     for i, p in enumerate(coords):
#         pd = line.project(Point(p))
#         if pd == distance:
#             return [LineString(coords[:i+1]),
#                     LineString(coords[i:])]
#         if pd > distance:
#             cp = line.interpolate(distance)
#             return [LineString(coords[:i] + [(cp.x, cp.y)]),
#                     LineString([(cp.x, cp.y)] + coords[i:])]



# def get_angle(p1, p2):
#     return degrees(atan2(p2[1]-p1[1], p2[0]-p1[0]))


# from pyproj import Transformer

# TRAN_4326_TO_3857 = Transformer.from_crs("EPSG:4326", "EPSG:3857")

# def transform_lonlat_webmercator(lon, lat):
#   return TRAN_4326_TO_3857.transform(lon, lat)

# def itransform_lonlat_webmercator(lonlat_points):
# #   return TRAN_4326_TO_3857.transform(lon, lat)
#   return TRAN_4326_TO_3857.itransform(lonlat_points)

