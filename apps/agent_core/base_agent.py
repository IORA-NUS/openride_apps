from orsim import ORSimAgent

class BaseAgent(ORSimAgent):
    def __init__(self, unique_id, run_id, reference_time, init_time_step, scheduler, behavior):
        super().__init__(unique_id, run_id, reference_time, init_time_step, scheduler, behavior)
        self.app = self._init_app()

    def _init_app(self):
        raise NotImplementedError("Subclasses must implement _init_app")

    def process_payload(self, payload):
        if payload.get('action') == 'step':
            return self.step(payload.get('time_step'))
        return False

    def step(self, time_step):
        raise NotImplementedError("Subclasses must implement step")
