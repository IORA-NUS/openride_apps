from statemachine import StateMachine, State
from graphviz import Digraph

import json
from orsim.utils import StateMachineSerializer


import requests

def register_and_validate_statemachine(server_url, headers, domain, statemachine_name, statemachine_cls):
    """
    Registers and validates a statemachine definition with the server.
    Raises ValueError if existing definition does not match current.
    """
    # sm_name = sm_cls.__name__
    definition = StateMachineSerializer.serialize(statemachine_cls)
    endpoint = f"{server_url}/statemachine"
    params = {
        "where": json.dumps({
            "$and": [
                {"domain": domain},
                {"name": statemachine_name},
            ]
        })
    }

    # Check if statemachine exists
    resp = requests.get(endpoint, headers=headers, params=params)
    # print(f"GET {endpoint} with params {params} returned status {resp.status_code}")
    # print(resp.url)
    # print(f"Checked statemachine {statemachine_name} existence: {resp.status_code}")
    # print(f"Response content: {resp.text}")

    if len(resp.json()['_items']) == 0:
        # Not found, create
        data = {
            "domain": domain,
            "name": statemachine_name,
            "definition": definition
        }
        create_resp = requests.post(endpoint, headers=headers, json=data)
        if create_resp.status_code != 201:
            raise RuntimeError(f"Failed to create statemachine: {create_resp.text}")
        return "created"
    else:
    # elif resp.status_code == 200:
        # Exists, validate
        existing = resp.json()['_items'][0].get("definition", {})
        # print(f"{existing = }")
        # print(f"{definition = }")

        if existing != definition:
            raise ValueError(f"Statemachine definition mismatch for {statemachine_name} (domain={domain})")
        return "validated"
    # else:
    #     raise RuntimeError(f"Unexpected response: {resp.status_code} {resp.text}")

# Usage:
# from your_module import RidehailDriverTripStateMachine
# definition = serialize_statemachine(RidehailDriverTripStateMachine)
# print(json.dumps(definition, indent=2))
