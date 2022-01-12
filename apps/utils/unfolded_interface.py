import json
import pandas as pd
import geopandas as gpd
from unfolded.map_sdk import UnfoldedMap
# import UnfoldedMap
m = UnfoldedMap(height=500)

import geopandas as gpd

df = pd.read_csv('/Users/rajiv/Development/iora/python/openroad/ride_hailing/apps/output/20220106194305/demand.csv')

# with open('/Users/rajiv/Development/iora/python/openroad/ride_hailing/apps/output/20220106194305/routes.geojson') as file:
#     data = json.load(file)

#     df = pd.DataFrame(data['features'])

m.add_dataset({'data': df})

m
