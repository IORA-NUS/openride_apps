"""Runtime scaffolding for shared agent loops."""

from .message_queue import RoleMessageQueueMixin
from .role_app_base import RoleAppBase
from .agent_runtime_base import AgentRuntimeBase

__all__ = ["RoleMessageQueueMixin", "RoleAppBase", "AgentRuntimeBase"]
