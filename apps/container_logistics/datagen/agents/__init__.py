"""Agents (per role) that run their own Policy trees (plan: policy_strategy_refactor_plan.md)."""

from .base import Agent, Policy, PolicyContext
from .registry import (
    AGENT_REGISTRY,
    POLICY_REGISTRY,
    agent_class,
    default_policy_name,
    known_policies,
    register,
    resolve_policy,
)

__all__ = [
    "Agent",
    "Policy",
    "PolicyContext",
    "AGENT_REGISTRY",
    "POLICY_REGISTRY",
    "agent_class",
    "default_policy_name",
    "known_policies",
    "register",
    "resolve_policy",
]
