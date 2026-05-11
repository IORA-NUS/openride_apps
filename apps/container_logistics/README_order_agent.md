# Container Logistics: Order Agent

This doc describes the **Order agent + Order state machine** wiring and how to run the order-only simulation.

## What was added

- **Server resource**: `container_logistics_order`
  - **URL shape**: `/<domain>/<run_id>/order`
  - **Transition endpoint**: `PATCH /<domain>/<run_id>/order/<order_id>/<transition>`
  - **Files**:
    - `openride_server/openroad_platform/api/models/container_logistics/order.py`
    - `openride_server/openroad_platform/api/controllers/container_logistics/order_controller.py`
    - `openride_server/openroad_platform/api/views/container_logistics/order_view.py`

- **Apps (agent-side)**
  - **State machine**: `apps/container_logistics/statemachine/order_sm.py` (`OrderStateMachine`)
  - **Agent**: `apps/container_logistics/order/agent.py` (`OrderAgent`)
  - **App**: `apps/container_logistics/order/app.py` (`OrderApp`)
  - **Manager**: `apps/container_logistics/order/manager.py` (`OrderManager`)

- **Simulation runner**
  - `apps/simulation/run_order_sim.py`

## Critical prerequisite: the server on `11654` must run the repo code

If the server you are hitting returns **404** for the order endpoint, it usually means the process running on
`http://localhost:11654` is not using this repo’s `openride_server` code/venv.

### Quick check

```bash
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:11654/container-logistics-sim/test/order
```

- Expected: **401** (endpoint exists; needs auth)
- Bad: **404** (endpoint does not exist on that server instance)

### Start the correct server (example)

Run gunicorn from the **`openride_server` venv**:

```bash
cd /home/user/openride_server/openroad_platform
../venv/bin/gunicorn --log-level=INFO --limit-request-line 0 -w 1 -k eventlet -b 0.0.0.0:11654 wsgi:app --reload
```

## Running the order-only simulation

Always run from the `openride_apps` repo root so imports resolve:

```bash
cd /home/user/openride_apps
./venv/bin/python -m apps.simulation.run_order_sim
```

### Overriding the API base URL

If you need to point to a different OpenRide server:

```bash
cd /home/user/openride_apps
OPENRIDE_SERVER_URL="http://localhost:11654" ./venv/bin/python -m apps.simulation.run_order_sim
```

## Expected output

The runner prints a JSON summary similar to:

```json
{
  "run_id": "order_run_YYYYMMDD_HHMMSS",
  "order_unique_id": "order_000001",
  "order_resource_id": "<mongo_id>",
  "assigned": true,
  "pickup_started": true,
  "pickup_done": true,
  "dropoff_started": true,
  "delivered": true,
  "terminal_state": "completed"
}
```

## Notes / conventions

- The order agent is **event-driven** in this first pass.
- Order state is advanced by `ORDER_WORKFLOW_EVENT` messages with `data.order_id`.
- The haul-trip side now includes `data.order_id` for all `order_*` workflow events so orders can filter messages.

