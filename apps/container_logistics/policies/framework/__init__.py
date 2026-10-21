"""Public API of the policy framework: slots, ``@policy``, typed params, lookups, resolution.

Authors import from here::

    from apps.container_logistics.policies.framework import PolicyParams, param, policy

The catalog is NOT re-exported (import ``framework.catalog`` directly). The catalog and
``framework.components`` are the only framework modules that reach into ``assignment``
(locally, at call time). Importing this package imports ``components``, which
registers the policy-reference resolvers that ``PolicyParams`` uses.
Leaf package (plan ``docs/plans/policy_slots_p0_p3.md`` §2.7): importing it never
imports ``apps.container_logistics.assignment.*``.
"""

from __future__ import annotations

from .errors import (
    DispatchContractViolation,
    PolicyDependencyError,
    PolicyError,
    PolicyParamsError,
    PolicyRegistrationError,
    PolicyTimingError,
    SlotNotImplementedError,
    UnknownPolicyError,
    UnknownSlotError,
)
from .params import SUPPORTED_SCHEMA_KEYWORDS, PolicyParams, PolicyRef, param, ref
from .registry import (
    PolicySpec,
    default_policy_name,
    get_policy_spec,
    list_policies,
    policy,
    spec_for_class,
)
from . import components  # noqa: F401  (registers the ref resolvers into params)
from .resolve import (
    ResolvedPolicy,
    authored_after_resolution,
    merge_policy_overrides,
    parse_policy_cli_args,
    policies_stamp,
    resolve_policies_block,
)
from .slots import SLOTS, SlotSpec, slot_spec

__all__ = [
    "policy",
    "PolicyParams",
    "param",
    "PolicyRef",
    "ref",
    "SUPPORTED_SCHEMA_KEYWORDS",
    "PolicySpec",
    "get_policy_spec",
    "list_policies",
    "default_policy_name",
    "spec_for_class",
    "SLOTS",
    "SlotSpec",
    "slot_spec",
    "ResolvedPolicy",
    "resolve_policies_block",
    "authored_after_resolution",
    "merge_policy_overrides",
    "parse_policy_cli_args",
    "policies_stamp",
    "PolicyError",
    "UnknownSlotError",
    "SlotNotImplementedError",
    "UnknownPolicyError",
    "PolicyParamsError",
    "PolicyTimingError",
    "PolicyDependencyError",
    "PolicyRegistrationError",
    "DispatchContractViolation",
]
