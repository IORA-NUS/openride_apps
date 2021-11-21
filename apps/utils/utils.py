import string
import random
from  collections import Mapping

from datetime import datetime


def id_generator(size=12, chars=string.ascii_lowercase + string.ascii_uppercase + string.digits):
    return ''.join(random.SystemRandom().choice(chars) for _ in range(size))


def is_success(status_code):
    return (status_code >= 200) and (status_code <= 299)


def deep_update(source, overrides):
    """
    Update a nested dictionary or similar mapping.
    Modify ``source`` in place.
    """
    for key, value in overrides.items():
        if isinstance(value, Mapping) and value:
            returned = deep_update(source.get(key, {}), value)
            source[key] = returned
        else:
            source[key] = overrides[key]
    return source


def time_to_str(time_var):
    return datetime.strftime(time_var, "%a, %d %b %Y %H:%M:%S GMT")

def str_to_time(time_str):
    return datetime.strptime(time_str, "%a, %d %b %Y %H:%M:%S GMT")

