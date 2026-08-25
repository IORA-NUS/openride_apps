from typing import Any

from apps.common.resource_client_mixin import ResourceClientMixin
from apps.config import simulation_domains, settings
from apps.container_logistics.statemachine import OrderStateMachine
from orsim.lifecycle import ORSimManager
from apps.common.resource_transition_client import ResourceTransitionClient
from apps.utils import is_success


class OrderManager(ResourceClientMixin, ORSimManager):
    def __init__(self, run_id, sim_clock, user, profile=None, persona=None):
        self.run_id = run_id
        self.user = user
        self.profile = profile or {}
        self.persona = {"role": "order", **(persona or {})}
        self.simulation_domain = simulation_domains.get("container_logistics", "container-logistics-sim")
        self._resource_client = ResourceTransitionClient()
        data = {
            "profile": self.profile,
            "persona": self.persona,
            "statemachine": {
                "name": OrderStateMachine.__name__,
                "domain": self.simulation_domain,
            },
            "state": OrderStateMachine.initial_state.name,
            "sim_clock": sim_clock,
        }
        self.resource = self.init_resource(sim_clock, data=data)

    def on_init(self):
        pass

    def login(self, sim_clock: Any) -> Any:
        """Orders use ``OrderStateMachine``, not ``WorkflowStateMachine``; skip generic dormant/offline/online login."""
        return self.resource

    def logout(self, sim_clock: Any) -> Any:
        """Orders reach a terminal OrderStateMachine state (completed/cancelled) on their own.

        The generic ``ORSimManager.logout`` attempts a ``WorkflowStateMachine`` 'offline'
        transition, which is invalid here and raised "<state> is not a valid state value"
        on every finished order. Orders hold no external resource to release, so this is a
        no-op — mirrors the ``login`` override above.
        """
        return self.resource

    def as_dict(self):
        return self.resource

    def get_id(self):
        return self.resource.get("_id")

    def refresh(self):
        self.resource = self.resource_get(resource_id=self.resource.get("_id"))
        return self.resource

    def _order_collection_url(self):
        return f"{settings['OPENRIDE_SERVER_URL']}/{self.simulation_domain}/{self.run_id}/order"

    def _order_item_url(self, suffix=None):
        if self.resource is None:
            raise Exception("order resource is not set")
        base = f"{self._order_collection_url()}/{self.resource['_id']}"
        return f"{base}/{suffix}" if suffix else base

    def _patch_order_transition(self, transition: str, payload: dict):
        if self.resource is None:
            raise Exception("order resource is not set")
        response = self._resource_client.patch(
            self._order_item_url(suffix=transition),
            headers=self.user.get_headers(etag=self.resource["_etag"]),
            payload=payload,
        )
        if is_success(response.status_code):
            self.refresh()
        return response

    def create_order(self, payload):
        return payload

    def publish(self, sim_clock, **extra):
        return self._patch_order_transition(OrderStateMachine.publish.name, {"sim_clock": sim_clock, **extra})

    def assign_to_truck(self, sim_clock, truck_id=None, **extra):
        payload = {"sim_clock": sim_clock, **extra}
        if truck_id is not None:
            payload["truck"] = truck_id
        return self._patch_order_transition(OrderStateMachine.assign.name, payload)

    def mark_pickup_started(self, sim_clock, **extra):
        return self._patch_order_transition(OrderStateMachine.pickup_started.name, {"sim_clock": sim_clock, **extra})

    def mark_pickup_done(self, sim_clock, **extra):
        return self._patch_order_transition(OrderStateMachine.pickup_done.name, {"sim_clock": sim_clock, **extra})

    def mark_dropoff_started(self, sim_clock, **extra):
        return self._patch_order_transition(OrderStateMachine.dropoff_started.name, {"sim_clock": sim_clock, **extra})

    def mark_delivered(self, sim_clock, **extra):
        return self._patch_order_transition(OrderStateMachine.deliver.name, {"sim_clock": sim_clock, **extra})

    def cancel(self, sim_clock, **extra):
        return self._patch_order_transition(OrderStateMachine.cancel.name, {"sim_clock": sim_clock, **extra})
