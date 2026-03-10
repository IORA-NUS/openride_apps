
__all__ = ["AssignmentAgentIndie", "AssignmentManager", "EngineManager"]


def __getattr__(name):
	if name == "AssignmentAgentIndie":
		from .assignment_agent_indie import AssignmentAgentIndie

		return AssignmentAgentIndie
	if name in {"AssignmentManager", "EngineManager"}:
		from .engine_manager import AssignmentManager, EngineManager

		return AssignmentManager if name == "AssignmentManager" else EngineManager
	raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
