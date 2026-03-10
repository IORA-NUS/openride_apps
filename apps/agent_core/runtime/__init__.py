"""Runtime scaffolding for shared agent loops."""

from .message_queue import RoleMessageQueueMixin
from .role_app_base import RoleAppBase

__all__ = ["RoleMessageQueueMixin", "RoleAppBase"]
