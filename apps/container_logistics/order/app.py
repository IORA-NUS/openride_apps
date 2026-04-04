from orsim.lifecycle import ORSimApp

from apps.common.user_registry import UserRegistry

from .manager import OrderManager


class OrderApp(ORSimApp):
    def _create_user(self):
        return UserRegistry(self.sim_clock, self.credentials)

    def _create_manager(self):
        return OrderManager(
            run_id=self.run_id,
            sim_clock=self.sim_clock,
            user=self.user,
            profile=self.behavior.get("profile", {}),
            persona=self.behavior.get("persona", {}),
        )
