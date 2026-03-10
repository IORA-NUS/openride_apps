from .message_queue import RoleMessageQueueMixin


class RoleAppBase(RoleMessageQueueMixin):
    """Shared app-level runtime fields and helpers for role apps."""

    def __init__(self, run_id, sim_clock, current_loc, messenger):
        self.run_id = run_id
        self.messenger = messenger
        self.message_queue = []
        self.topic_params = {}
        self.latest_sim_clock = sim_clock
        self.latest_loc = current_loc

    def update_current(self, sim_clock, current_loc):
        self.latest_sim_clock = sim_clock
        self.latest_loc = current_loc
