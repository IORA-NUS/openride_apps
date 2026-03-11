from .message_queue import RoleMessageQueueMixin
from typing import Any


class RoleAppBase(RoleMessageQueueMixin):
    """Shared app-level runtime fields and helpers for role apps."""

    def __init__(self, run_id: str, sim_clock: Any, current_loc: Any, messenger: Any) -> None:
        self.run_id = run_id
        self.messenger = messenger
        self.message_queue = []
        self.topic_params = {}
        self.latest_sim_clock = sim_clock
        self.latest_loc = current_loc

    def update_current(self, sim_clock: Any, current_loc: Any) -> None:
        self.latest_sim_clock = sim_clock
        self.latest_loc = current_loc
