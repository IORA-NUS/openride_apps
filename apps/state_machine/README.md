# State Machine Documentation

This folder contains state machine definitions and generated Graphviz diagrams for supported workflows.

## Included Domains
- `ride_hail/`: Driver and passenger trip state machines.
- `container_logistics/`: Haulier and facility interaction state machines.
- `hl_delivery/`: Hyperlocal delivery state machines.

## Diagram Generator
Use `generate_state_machine_svgs.py` to regenerate diagrams from the current state machine classes.

Important:
- The generator uses `python-statemachine` built-in `DotGraphMachine`.
- Rendering is done by `pydot` and Graphviz (`dot` binary).
- Diagram structure comes from the state machine classes directly (no hardcoded state/transition lists).
- When class definitions change, regenerate diagrams to keep docs in sync.

Prerequisites:
- `pydot` installed in the project environment.
- Graphviz installed and `dot` available on `PATH`.

## Run Instructions
From repo root (`openride_apps/openride_apps`):

```bash
/Users/rajiv/Development/iora/openroad/openride_apps/env-apps/bin/python apps/state_machine/generate_state_machine_svgs.py
```

Alternative (absolute script path):

```bash
/Users/rajiv/Development/iora/openroad/openride_apps/env-apps/bin/python -B /Users/rajiv/Development/iora/openroad/openride_apps/openride_apps/apps/state_machine/generate_state_machine_svgs.py
```

## Output Files
Generated diagrams are written as `.png` files to:
- `apps/state_machine/ride_hail/diagrams/`
- `apps/state_machine/container_logistics/diagrams/`

## When To Regenerate
Regenerate diagrams whenever you change:
- state names
- transition events
- transition source/target states
- initial states
- included state machine classes in `generate_state_machine_svgs.py`
