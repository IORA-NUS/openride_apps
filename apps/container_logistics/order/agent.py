from orsim.lifecycle import ORSimAgent

from .app import OrderApp


class OrderAgent(ORSimAgent):
    def _create_app(self):
        return OrderApp(
            run_id=self.run_id,
            sim_clock=self.get_current_time_str(),
            behavior=self.behavior,
            messenger=self.messenger,
            agent_helper=self,
        )

    @property
    def process_payload_on_init(self):
        return True
