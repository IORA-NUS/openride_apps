"""Error types for the policy framework.

Every configuration error subclasses :class:`PolicyError`, which subclasses
``ValueError`` so the existing ``except ValueError`` paths (compile -> ``INVALID``)
keep catching them. At compile time callers wrap them into ``SpecValidationError``
(plan §2.5). Messages name the offending key and value, the allowed set and the fix.

Leaf module: stdlib only, imports nothing from the app (plan §2.7).
"""

from __future__ import annotations

__all__ = [
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


class PolicyError(ValueError):
    """Base class for every policy-framework error."""


class UnknownSlotError(PolicyError):
    """A slot name that is not in ``SLOTS``."""


class SlotNotImplementedError(PolicyError):
    """A slot that is declared (legacy or planned) but has no registry-backed policies."""


class UnknownPolicyError(PolicyError):
    """A policy name that is not registered in its slot."""


class PolicyParamsError(PolicyError):
    """Policy params that fail their declared types, bounds or choices."""


class PolicyTimingError(PolicyError):
    """A timing the slot does not declare, or declares but cannot execute yet."""


class PolicyDependencyError(PolicyError):
    """A policy whose ``requires`` modules are not importable."""


class PolicyRegistrationError(PolicyError):
    """A malformed ``@policy`` declaration; raised at import (registration) time."""


class DispatchContractViolation(PolicyError):
    """A dispatch plan that breaks the dispatch contract (P1 invariants)."""
