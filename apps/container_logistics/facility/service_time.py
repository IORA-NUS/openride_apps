"""Resolve facility gate service duration from behavior/profile dicts."""


def resolve_service_time(behavior: dict | None, profile: dict | None = None) -> int:
    """Return configured facility service time in seconds."""
    behavior = behavior or {}
    profile = profile or behavior.get("profile") or {}

    for key in ("service_time",):
        for source in (behavior, profile):
            val = source.get(key)
            if val is not None:
                try:
                    return max(0, int(val))
                except (TypeError, ValueError):
                    pass

    # Legacy configs used leg-specific keys with identical values.
    legacy = []
    for source in (behavior, profile):
        for key in ("pickup_service_time", "dropoff_service_time"):
            val = source.get(key)
            if val is not None:
                try:
                    legacy.append(int(val))
                except (TypeError, ValueError):
                    pass
    if legacy:
        return max(0, max(legacy))
    return 0
