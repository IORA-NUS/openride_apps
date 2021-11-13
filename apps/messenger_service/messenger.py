
import json
from apps.config import settings
import requests
import urllib3
from urllib.parse import quote
import logging

import paho.mqtt.client as paho


class Messenger:

    def __init__(self, credentials, channel_id=None, on_message=None, transport=None):
        ''' '''
        self.credentials = credentials
        self.channel_id = channel_id

        if transport is None:
            self.client = paho.Client(credentials['email'])
            self.client.username_pw_set(username=self.credentials['email'], password=self.credentials['password'])
            Messenger.register_user(self.credentials['email'], self.credentials['password'])

            self.client.connect(settings['MQTT_BROKER'])

        if on_message is not None:
            self.client.on_message = on_message


        # RabbitMQ PubSub queue is used for processing requests in sequence
        # This is a deliberate design choice to enable:
        #   - Inter-Agent communication as core part of system design

        if channel_id is not None:
            self.client.loop_start()
            self.client.subscribe(f"{channel_id}", qos=0)
            logging.info(f"Channel: {channel_id}")


    def disconnect(self):
        if self.channel_id is not None:
            self.client.unsubscribe(self.channel_id)


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
            except Exception as e:
                logging.exception(str(e))
                raise(e)

        # reset the user and set appropriate permissions as needed
        quoted_slash = '%2F'
        response = requests.put(f"{settings['RABBITMQ_MANAGEMENT_SERVER']}/permissions/{quoted_slash}/{username}",
                        data=json.dumps({"username":username, "vhost":"/", "configure":".*", "write":".*", "read":".*"}),
                        headers={"content-type": "application/json"},
                        auth=(settings['RABBITMQ_ADMIN_USER'], settings['RABBITMQ_ADMIN_PASSWORD'])
                    )

        response = requests.put(f"{settings['RABBITMQ_MANAGEMENT_SERVER']}/topic-permissions/{quoted_slash}/{username}",
                        data=json.dumps({username: username, "vhost": "/", "exchange": "", "write": ".*", "read": ".*"}),
                        headers={"content-type": "application/json"},
                        auth=(settings['RABBITMQ_ADMIN_USER'], settings['RABBITMQ_ADMIN_PASSWORD'])
                    )

