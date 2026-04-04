import os, sys, json
current_path = os.path.abspath('.')
parent_path = os.path.dirname(current_path)
sys.path.append(parent_path)

import logging
from random import random
from .app import AnalyticsApp

from datetime import datetime
from dateutil.relativedelta import relativedelta

from orsim.lifecycle import ORSimAgent


class AnalyticsAgentIndie(ORSimAgent):
    ''' '''

    def _create_app(self):
        return AnalyticsApp(run_id=self.run_id,
                         sim_clock=self.get_current_time_str(),
                         behavior=self.behavior,
                         messenger=self.messenger,
                        )

    @property
    def process_payload_on_init(self):
        return False

    def entering_market(self, time_step):
        self.active = True

    def exiting_market(self):
        self.active = False

    def logout(self):
        def step(self, time_step):
            logging.info(f"[AnalyticsAgentIndie.step] Called for agent {self.unique_id} at time_step {time_step}")
        self.app.close(self.get_current_time_str())

    def estimate_next_event_time(self):
        return self.current_time

    def step(self, time_step):
        # print(f"[AnalyticsAgentIndie.step] Called for agent {self.unique_id} at time_step {time_step}")
        if (self.current_time_step % self.behavior['steps_per_action'] == 0) and \
                    (random() <= self.behavior['response_rate']) and \
                    (self.next_event_time <= self.current_time):
            # print(f"[AnalyticsAgentIndie.step] Agent {self.unique_id} is taking action at time_step {time_step}. Conditions met.")
            # print("before compute_all_metrics")
            try:
                self.compute_all_metrics()
            except Exception as e:
                logging.exception(f"Error computing metrics for agent {self.unique_id} at time_step {time_step}: {str(e)}")
                # raise e
            # print("after compute_all_metrics")

            run_data_dir = os.path.join(self.datahub_dir, self.behavior.get('domain', 'ridehail'), 'run_data', str(self.run_id))
            # if run_data_dir is None:
            #     run_data_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'datahub', 'run_data', str(self.run_id))
            if not os.path.exists(run_data_dir):
                os.makedirs(run_data_dir)
            # Directory is now guaranteed to exist

            if self.behavior.get('profile', {}).get('publish_realtime_data', False):
                location_stream, route_stream = self.publish_active_trips(self.get_current_time_str())

                if self.behavior.get('profile', {}).get('write_ws_output_to_file', False):
                    stream_output_dir = os.path.join(run_data_dir, 'stream')
                    if not os.path.exists(stream_output_dir):
                        os.makedirs(stream_output_dir)

                    with open(os.path.join(stream_output_dir, f'{self.current_time_step}.location_stream.json'), 'w') as publish_file:
                        publish_file.write(json.dumps(location_stream))

                    with open(os.path.join(stream_output_dir, f'{self.current_time_step}.route_stream.json'), 'w') as publish_file:
                        publish_file.write(json.dumps(route_stream))

            if self.behavior.get('profile', {}).get('publish_paths_history', False):
                if (((self.current_time_step + 1) * self.orsim_settings['STEP_INTERVAL']) % self.behavior.get('profile', {}).get('paths_history_time_window', 0)) == 0:
                    timewindow_end = self.current_time
                    timewindow_start = timewindow_end - relativedelta(seconds=self.behavior['paths_history_time_window'] + self.orsim_settings['STEP_INTERVAL'])
                    logging.debug(f"{timewindow_start}, {timewindow_end}")

                    paths_history = self.app.get_history_as_paths(timewindow_start, timewindow_end)

                    if self.behavior.get('profile', {}).get('write_ph_output_to_file', False):
                        rest_output_dir = os.path.join(run_data_dir, 'rest')
                        if not os.path.exists(rest_output_dir):
                            os.makedirs(rest_output_dir)

                        with open(os.path.join(rest_output_dir, f'{self.current_time_step}.paths_history.json'), 'w') as publish_file:
                            publish_file.write(json.dumps(paths_history))

            return True
        else:
            # print(f"[AnalyticsAgentIndie.step] Agent {self.unique_id} is skipping this step. Conditions not met.")
            # print()
            return False

    def publish_active_trips(self, sim_clock):
        """Publish active driver and passenger trips to the appropriate stream using the agent's messenger."""
        driver_trips = self.app.get_active_driver_trips(sim_clock)
        passenger_trips = self.app.get_active_passenger_trips(sim_clock)
        from apps.loc_service import transform_lonlat_webmercator, itransform_lonlat_webmercator
        import json
        import logging
        location_stream = {
            "type": "featureResult",
            "features": []
        }
        route_stream = {
            "type": "featureResult",
            "features": []
        }
        for id, trip in driver_trips.items():
            current_loc = trip['current_loc']
            transformed_loc = transform_lonlat_webmercator(current_loc['coordinates'][1], current_loc['coordinates'][0])
            driver_feature = {
                "attributes": {
                    "OBJECTID": trip['last_waypoint_id'],
                    "TRACKID": id,
                    "CLASS": 'driver',
                    "STATUS": trip['state']
                },
                "geometry": {
                    "x": transformed_loc[0],
                    "y": transformed_loc[1]
                }
            }
            location_stream['features'].append(driver_feature)
            if (trip.get('projected_path') is not None) and (len(trip.get('projected_path')) > 1):
                projected_path = trip['projected_path']
                transformed_projected_path = itransform_lonlat_webmercator([[item[1], item[0]] for item in projected_path])
                driver_feature = {
                    "attributes": {
                        "OBJECTID": trip['last_waypoint_id'],
                        "TRACKID": id,
                        "CLASS": 'driver',
                        "STATUS": trip['state']
                    },
                    "geometry": {
                        "paths": [list(transformed_projected_path)]
                    }
                }
                route_stream['features'].append(driver_feature)
        for id, trip in passenger_trips.items():
            current_loc = trip['current_loc']
            transformed_loc = transform_lonlat_webmercator(current_loc['coordinates'][1], current_loc['coordinates'][0])
            passenger_feature = {
                "attributes": {
                    "OBJECTID": trip['last_waypoint_id'],
                    "TRACKID": id,
                    "CLASS": 'passenger',
                    "STATUS": trip['state']
                },
                "geometry": {
                    "x": transformed_loc[0],
                    "y": transformed_loc[1]
                }
            }
            location_stream['features'].append(passenger_feature)
        from apps.config import settings
        if settings['WEBSOCKET_SERVICE'] == 'MQTT':
            self.messenger.client.publish(f'anaytics/location_stream', json.dumps(location_stream))
            self.messenger.client.publish(f'anaytics/route_stream', json.dumps(route_stream))
        elif settings['WEBSOCKET_SERVICE'] == 'WS':
            import websockets, asyncio
            async def publish_location_stream_async(location_stream):
                uri = f"{settings['WS_SERVER']}/location_stream"
                async with websockets.connect(uri) as websocket:
                    await websocket.send(json.dumps(location_stream))
            async def publish_route_stream_async(route_stream):
                uri = f"{settings['WS_SERVER']}/route_stream"
                async with websockets.connect(uri) as websocket:
                    await websocket.send(json.dumps(route_stream))
            asyncio.run(publish_location_stream_async(location_stream))
            asyncio.run(publish_route_stream_async(route_stream))
        return location_stream, route_stream

    def compute_all_metrics(self):
        start_time = self.current_time - relativedelta(seconds=(self.behavior['steps_per_action'] * self.orsim_settings['STEP_INTERVAL']))
        end_time = self.current_time

        self.app.compute_all_metrics(start_time, end_time)

