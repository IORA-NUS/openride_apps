
import json
from apps.config import settings
import requests
import urllib3
from urllib.parse import quote
import logging

import paho.mqtt.client as paho


class Messenger:

    def __init__(self, run_id, credentials, channel_id=None, on_message=None, transport=None):
        ''' '''
        self.run_id = run_id
        self.credentials = credentials

        # print('registering messenger')

        if transport is None:
            self.client = paho.Client(credentials['email'])
            self.client.username_pw_set(username=self.credentials['email'], password=self.credentials['password'])
            Messenger.register_user(self.credentials['email'], self.credentials['password'])
            self.client.connect(settings['MQTT_BROKER'])
        # else:
        #     self.client = paho.Client(credentials['email'], transport=transport)
        #     self.client.username_pw_set(username=self.credentials['email'], password=self.credentials['password'])
        #     Messenger.register_user(self.credentials['email'], self.credentials['password'])
        #     self.client.connect(settings['MQTT_BROKER'], settings['WEB_MQTT_PORT'])
        #     # self.client.tls_set()

        if on_message is not None:
            self.client.on_message = on_message


        # RabbitMQ PubSub queue is used for processing requests in sequence
        # This is a deliberate design choice to enable:
        #   - Inter-Agent communication as core part of system design

        if channel_id is not None:
            self.client.loop_start()
            self.client.subscribe(f"{self.run_id}/{channel_id}")
            print(f"Channel: {self.run_id}/{channel_id}")

    # def subscribe(self, channel_id):
    #     if channel_id is not None:
    #         self.client.loop_start()
    #         self.client.subscribe(f"Agent/{channel_id}")


    @classmethod
    def register_user(cls, username, password):
        ''' '''

        response = requests.get(f"{settings['RABBITMQ_MANAGEMENT_SERVER']}/users/{username}")
        if (response.status_code >= 200) and (response.status_code <= 299):
            logging.warning('User is already registered')
        else:
            try:
                response = requests.put(f"{settings['RABBITMQ_MANAGEMENT_SERVER']}/users/{username}",
                                        data=json.dumps({'password': password, 'tags': ''}),
                                        headers={"content-type": "application/json"},
                                        auth=(settings['RABBITMQ_ADMIN_USER'], settings['RABBITMQ_ADMIN_PASSWORD'])
                                    )
                # print(response.text)
            except Exception as e:
                # print(e)
                logging.exception(str(e))
                raise(e)

        # reset the user and set appropriate permissions as needed
        quoted_slash = '%2F'
        response = requests.put(f"{settings['RABBITMQ_MANAGEMENT_SERVER']}/permissions/{quoted_slash}/{username}",
                        data=json.dumps({"username":username, "vhost":"/", "configure":".*", "write":".*", "read":".*"}),
                        headers={"content-type": "application/json"},
                        auth=(settings['RABBITMQ_ADMIN_USER'], settings['RABBITMQ_ADMIN_PASSWORD'])
                    )
        # print(response.url)
        # print(response.text)

        response = requests.put(f"{settings['RABBITMQ_MANAGEMENT_SERVER']}/topic-permissions/{quoted_slash}/{username}",
                        data=json.dumps({username: username, "vhost": "/", "exchange": "", "write": ".*", "read": ".*"}),
                        headers={"content-type": "application/json"},
                        auth=(settings['RABBITMQ_ADMIN_USER'], settings['RABBITMQ_ADMIN_PASSWORD'])
                    )
        # print(response.text)

