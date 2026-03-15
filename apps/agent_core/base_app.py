from typing import Any, Dict

class BaseApp:
    def __init__(self, run_id: str, start_time: str, credentials: Dict[str, Any], messenger=None):
        self.run_id = run_id
        self.start_time = start_time
        self.credentials = credentials
        self.messenger = messenger

    def login(self, *args, **kwargs):
        raise NotImplementedError

    def logout(self, *args, **kwargs):
        raise NotImplementedError

    def refresh(self, *args, **kwargs):
        raise NotImplementedError

    # Add other common app methods as needed
