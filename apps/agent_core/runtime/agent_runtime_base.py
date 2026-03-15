import logging
import traceback
from typing import Any, Dict


class AgentRuntimeBase:
    """Shared step envelope for ORSim role agents."""

    # def process_payload(self, payload: Dict[str, Any]) -> bool:
    #     did_step: bool = False

    #     if (payload.get("action") == "step") or (payload.get("action") == "init"):
    #         self.add_step_log("Before entering_market")
    #         self.entering_market(payload.get("time_step"))
    #         self.add_step_log("After entering_market")

    #         if self.is_active():
    #             try:
    #                 self.add_step_log("Before step")
    #                 did_step = self.step(payload.get("time_step"))
    #                 self.add_step_log("After step")
    #                 self.failure_count = 0
    #                 self.failure_log = {}
    #             except Exception:
    #                 self.failure_log[self.failure_count] = traceback.format_exc()
    #                 self.failure_count += 1

    #         self.add_step_log("Before exiting_market")
    #         self.exiting_market()
    #         self.add_step_log("After exiting_market")
    #     else:
    #         logging.error(f"{payload = }")

    #     return did_step
