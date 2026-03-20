from typing import Any, Dict
from orsim.messenger import Messenger
import logging


class BaseApp:
    def __init__(self, run_id: str, sim_clock: str, credentials: Dict[str, Any], messenger=None, **kwargs):
        """
        Base class for all app modules.
        Args:
            run_id: Unique run identifier
            sim_clock: Simulation clock time
            credentials: Auth credentials dict
            messenger: Messaging interface (optional)
            kwargs: Additional fields for subclass customization
        """
        self.run_id = run_id
        self.sim_clock = sim_clock
        self.credentials = credentials
        if messenger is not None:
            if Messenger is None or not isinstance(messenger, Messenger):
                raise TypeError("messenger must be an instance of orsim.messenger.Messenger")
        self.messenger = messenger

        for k, v in kwargs.items():
            setattr(self, k, v)

        self.user = self.create_user()
        self.manager = self.create_manager()
        # self.register_topic_handlers()

        if self.manager:
            self.topic_params = {
                f"{self.run_id}/{self.manager.get_id()}": self.message_handler
            }
        else:
            self.topic_params = {}
        self.message_queue = []
        self.exited_market = False

    # logger and manager/user_registry are implementation-specific and should be set in subclasses

    def create_user(self):
        ''' '''
        raise NotImplementedError

    def create_manager(self):
        ''' '''
        raise NotImplementedError

    def launch(self, sim_clock, **kwargs):
        if self.manager:
            try:
                self.manager.login(sim_clock)
            except Exception as e:
                logging.warning(f"Failed to login manager: {str(e)}")

    def close(self, sim_clock):
        self.exited_market = True
        if self.manager:
            try:
                self.manager.logout(sim_clock)
            except Exception as e:
                logging.warning(f"Failed to logout {self.get_manager()}: {str(e)}")

    # def refresh(self, *args, **kwargs):
    #     raise NotImplementedError

    def update_current(self, sim_clock, current_loc):
        self.latest_sim_clock = sim_clock
        self.latest_loc = current_loc

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
        except IndexError:
            return None

    def enfront_message(self, payload):
        self.message_queue.insert(0, payload)


    def get_manager(self):
        return self.manager.as_dict() if self.manager else None
