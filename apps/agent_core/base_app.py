from typing import Any, Dict

class BaseApp:
    def __init__(self, run_id: str, start_time: str, credentials: Dict[str, Any], messenger=None, **kwargs):
        """
        Base class for all app modules.
        Args:
            run_id: Unique run identifier
            start_time: Simulation start time
            credentials: Auth credentials dict
            messenger: Messaging interface (optional)
            kwargs: Additional fields for subclass customization
        """
        self.run_id = run_id
        self.start_time = start_time
        self.credentials = credentials
        self.messenger = messenger

        self.topic_params = {}
        self._message_queue = []
        self.exited_market = False
        for k, v in kwargs.items():
            setattr(self, k, v)

        self.user = self.create_user()
        self.manager = self.create_manager()
        # self.register_topic_handlers()

    # logger and manager/user_registry are implementation-specific and should be set in subclasses

    def create_user(self):
        ''' '''
        raise NotImplementedError

    def create_manager(self):
        ''' '''
        raise NotImplementedError

    def launch(self, *args, **kwargs):
        raise NotImplementedError

    def close(self, *args, **kwargs):
        self.exited_market = True

    def refresh(self, *args, **kwargs):
        raise NotImplementedError

    # def register_topic_handlers(self):
    #     raise NotImplementedError

    def message_handler(self, *args, **kwargs):
        raise NotImplementedError

    def enqueue_message(self, payload):
        ''' '''
        self.message_queue.append(payload)

    def dequeue_message(self):
        ''' '''
        try:
            return self.message_queue.pop(0)
        except: return None

    def enfront_message(self, payload):
        self.message_queue.insert(0, payload)

    # def get_entity(self):
    #     """
    #     Returns the main entity for this app, following domain app conventions.
    #     By default, returns manager.as_dict() if manager exists, else None.
    #     Subclasses may override for custom entity logic.
    #     """
    #     if hasattr(self, 'manager') and self.manager:
    #         return self.manager.as_dict()
    #     return None

    def get_manager(self):
        return self.manager.as_dict() if self.manager else None
