import requests
import json
from apps.utils import is_success
from apps.config import settings

class ResourceClientMixin:
    """
    Mixin for generic RESTful resource operations and URL construction.
    Expects self.run_id, self.settings, and self.user.
    """


    def _resource_url(self, resource_id=None):
        base = settings['OPENRIDE_SERVER_URL']
        url = f"{base}/{self.run_id}/{self.persona.get('role')}"
        if resource_id is not None:
            url = f"{url}/{resource_id}"
        return url



    def resource_get(self, resource_id=None, params={}, timeout=None):
        url = self._resource_url(resource_id)
        response = requests.get(
            url,
            headers=self.user.get_headers(),
            timeout=timeout or settings.get('NETWORK_REQUEST_TIMEOUT', 10),
            params=params
        )
        self._check_response(response)
        return response.json()

    # def resource_find(self, resource_id=None, timeout=None, params={}):
    #     url = self._resource_url(resource_id)
    #     response = requests.get(
    #         url,
    #         headers=self.user.get_headers(),
    #         timeout=timeout or settings.get('NETWORK_REQUEST_TIMEOUT', 10),
    #         params=params
    #     )
    #     self._check_response(response)
    #     return response.json()



    def resource_post(self, data, timeout=None):
        url = self._resource_url()
        response = requests.post(
            url,
            headers=self.user.get_headers(),
            data=json.dumps(data),
            timeout=timeout or settings.get('NETWORK_REQUEST_TIMEOUT', 10)
        )
        self._check_response(response)
        return response.json()



    def resource_patch(self, resource_id, data, etag=None, timeout=None):
        url = self._resource_url(resource_id)
        headers = self.user.get_headers(etag=etag)
        response = requests.patch(
            url,
            headers=headers,
            data=json.dumps(data),
            timeout=timeout or settings.get('NETWORK_REQUEST_TIMEOUT', 10)
        )
        self._check_response(response)
        self.resource_get(resource_id=resource_id)  # Refresh resource after patch
        return response.json()

    def _check_response(self, response):
        if not is_success(response.status_code):
            raise Exception(f"{response.url}, {response.text}")
