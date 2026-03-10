
__all__ = ["AssignmentAgentIndie"]


def __getattr__(name):
	if name == "AssignmentAgentIndie":
		from .assignment_agent_indie import AssignmentAgentIndie

		return AssignmentAgentIndie
	raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
