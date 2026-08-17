"""Default perf_stream tuning knobs (overridable via orsim_settings)."""

PERF_TICK_EVERY_STEP = True
PERF_DETAIL_INTERVAL_STEPS = 48
PERF_DETAIL_ON_NEW_MAX = True
PERF_SLOW_AGENT_TOP_N = 10
PERF_INCLUDE_PROCESS_METRICS = False
PERF_KAFKA_FLUSH_EVERY_STEPS = 10

# Backward-compatible alias for detail sampling interval.
PERF_KAFKA_INTERVAL_STEPS = PERF_DETAIL_INTERVAL_STEPS


def apply_perf_settings(settings: dict) -> dict:
    """Merge perf_stream defaults into an orsim_settings dict."""
    settings = dict(settings)
    settings.setdefault("PERF_TICK_EVERY_STEP", PERF_TICK_EVERY_STEP)
    settings.setdefault("PERF_DETAIL_INTERVAL_STEPS", PERF_DETAIL_INTERVAL_STEPS)
    settings.setdefault("PERF_DETAIL_ON_NEW_MAX", PERF_DETAIL_ON_NEW_MAX)
    settings.setdefault("PERF_SLOW_AGENT_TOP_N", PERF_SLOW_AGENT_TOP_N)
    settings.setdefault("PERF_INCLUDE_PROCESS_METRICS", PERF_INCLUDE_PROCESS_METRICS)
    settings.setdefault("PERF_KAFKA_FLUSH_EVERY_STEPS", PERF_KAFKA_FLUSH_EVERY_STEPS)
    settings.setdefault("PERF_KAFKA_INTERVAL_STEPS", PERF_KAFKA_INTERVAL_STEPS)
    return settings
