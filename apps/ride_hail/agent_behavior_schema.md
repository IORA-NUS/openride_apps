# ORSim Agent Behavior Data Schema

This document summarizes the required and agent-specific fields for initializing agents in the ridehail simulation, based on the contents of the `stay_or_leave_test` dataset and the agent initialization code.

## Common (Required) Fields for All Agents
- `email` (string): Agent's email address
- `password` (string): Agent's password
- `persona` (dict):
  - `domain` (string): Domain of the agent (e.g., 'ridehail-sim')
  - `role` (string): Role of the agent (e.g., 'driver', 'passenger', 'analytics', 'engine')
- `profile` (dict):
- `response_rate` (number): Probability of responding per step
- `step_only_on_events` (bool): Whether to step only on events
- `steps_per_action` (int): Number of steps per action

## Driver-Specific Fields
- `action_when_free` (string): What the driver does when free (e.g., 'random_walk')
- `coverage_area_name` (string): Name of the coverage area
- `empty_dest_loc` (dict): GeoJSON Point for empty destination
- `init_loc` (dict): GeoJSON Point for initial location
- `shift_start_time` (int): Start time of driver's shift
- `shift_end_time` (int): End time of driver's shift
- `transition_prob` (list): Transition probabilities for state machine
- `transition_time_dropoff` (int): Time for dropoff transition
- `transition_time_pickup` (int): Time for pickup transition
- `update_passenger_location` (bool): Whether to update passenger location

## Passenger-Specific Fields
- `pickup_loc` (dict): GeoJSON Point for pickup location
- `dropoff_loc` (dict): GeoJSON Point for dropoff location
- `trip_price` (number): Price of the trip
- `trip_request_time` (int): Time when trip is requested
- `transition_prob` (list): Transition probabilities for state machine

## Assignment Agent-Specific Fields
- `solver` (string): Name of the assignment solver
- `solver_params` (dict): Parameters for the solver

## Analytics Agent-Specific Fields
- `paths_history_time_window` (int): Time window for paths history
- `publish_paths_history` (bool): Whether to publish paths history
- `publish_realtime_data` (bool): Whether to publish real-time data
- `write_ph_output_to_file` (bool): Write paths history output to file
- `write_ws_output_to_file` (bool): Write websocket output to file

## Example Cerberus Schema (Python)

```
from cerberus import Validator

AGENT_BEHAVIOR_SCHEMA = {
    'email': {'type': 'string', 'required': True},
    'password': {'type': 'string', 'required': True},
    'persona': {
        'type': 'dict',
        'schema': {
            'domain': {'type': 'string', 'required': True},
            'role': {'type': 'string', 'required': True},
        },
        'required': True
    },
    'profile': {'type': 'dict', 'required': True},
    'response_rate': {'type': 'number', 'required': True},
    'step_only_on_events': {'type': 'boolean', 'required': True},
    'steps_per_action': {'type': 'integer', 'required': True},
    # Add agent-specific fields in subclasses or at runtime
}

def validate_behavior(behavior):
    v = Validator(AGENT_BEHAVIOR_SCHEMA)
    return v.validate(behavior), v.errors
```

## Field Summary Table

| Field                      | Common | Driver | Passenger | Assignment | Analytics |
|----------------------------|--------|--------|-----------|------------|-----------|
| email                      |   ✔    |   ✔    |     ✔     |     ✔      |     ✔     |
| password                   |   ✔    |   ✔    |     ✔     |     ✔      |     ✔     |
| persona                    |   ✔    |   ✔    |     ✔     |     ✔      |     ✔     |
| profile                    |   ✔    |   ✔    |     ✔     |     ✗      |     ✗     |
| response_rate              |   ✔    |   ✔    |     ✔     |     ✔      |     ✔     |
| step_only_on_events        |   ✔    |   ✔    |     ✔     |     ✗      |     ✔     |
| steps_per_action           |   ✔    |   ✔    |     ✔     |     ✔      |     ✔     |
| action_when_free           |   ✗    |   ✔    |     ✗     |     ✗      |     ✗     |
| coverage_area_name         |   ✗    |   ✔    |     ✗     |     ✗      |     ✗     |
| empty_dest_loc             |   ✗    |   ✔    |     ✗     |     ✗      |     ✗     |
| init_loc                   |   ✗    |   ✔    |     ✗     |     ✗      |     ✗     |
| shift_start_time           |   ✗    |   ✔    |     ✗     |     ✗      |     ✗     |
| shift_end_time             |   ✗    |   ✔    |     ✗     |     ✗      |     ✗     |
| transition_prob            |   ✗    |   ✔    |     ✔     |     ✗      |     ✗     |
| transition_time_dropoff    |   ✗    |   ✔    |     ✗     |     ✗      |     ✗     |
| transition_time_pickup     |   ✗    |   ✔    |     ✗     |     ✗      |     ✗     |
| update_passenger_location  |   ✗    |   ✔    |     ✗     |     ✗      |     ✗     |
| pickup_loc                 |   ✗    |   ✗    |     ✔     |     ✗      |     ✗     |
| dropoff_loc                |   ✗    |   ✗    |     ✔     |     ✗      |     ✗     |
| trip_price                 |   ✗    |   ✗    |     ✔     |     ✗      |     ✗     |
| trip_request_time          |   ✗    |   ✗    |     ✔     |     ✗      |     ✗     |
| solver                     |   ✗    |   ✗    |     ✗     |     ✔      |     ✗     |
| solver_params              |   ✗    |   ✗    |     ✗     |     ✔      |     ✗     |
| paths_history_time_window  |   ✗    |   ✗    |     ✗     |     ✗      |     ✔     |
| publish_paths_history      |   ✗    |   ✗    |     ✗     |     ✗      |     ✔     |
| publish_realtime_data      |   ✗    |   ✗    |     ✗     |     ✗      |     ✔     |
| write_ph_output_to_file    |   ✗    |   ✗    |     ✗     |     ✗      |     ✔     |
| write_ws_output_to_file    |   ✗    |   ✗    |     ✗     |     ✗      |     ✔     |

---

This document should be updated if new agent types or fields are introduced.
