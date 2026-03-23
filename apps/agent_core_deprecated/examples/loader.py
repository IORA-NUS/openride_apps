"""
Utility to load and validate agent configuration from JSON files.
"""
import json
import os

def load_agent_config(path=None):
    """
    Load agent configuration from a JSON file.
    If path is None, defaults to 'apps/agent_core/examples/agent_config.json'.
    """
    if path is None:
        path = os.path.join(os.path.dirname(__file__), 'agent_config.json')
    with open(path, 'r') as f:
        config = json.load(f)
    # Optionally, add validation here
    return config
