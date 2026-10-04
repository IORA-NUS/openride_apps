"""App-side messaging transports for the ORSim phase-1 transport seam.

The engine stays broker-agnostic; the Kafka implementation lives here, in the
app, because it needs app knowledge (the run id, the lane topic conventions, the
existing ``apps.utils.kafka_utils`` admin helpers) that ORSim has no business
carrying.

**This module imports NOTHING from ``confluent_kafka`` at module scope**, and
neither does :mod:`apps.transport.factory`.  ``from apps.transport import
build_agent_transport`` is therefore free on the paho path -- which is the
flag's default -- and cannot change import-time behaviour anywhere.  The C
extension is imported only when someone actually asks for the kafka transport.
:mod:`apps.transport.lane_topics` is likewise pure, so the mapping can be
unit-tested with zero infrastructure.

Layout:

``lane_topics``
    The whole ORSim-topic <-> Kafka-topic mapping.  Pure.  This is the file a
    reviewer reads to check the injectivity argument.
``lane_admin``
    Create / confirm / reap the three per-run lane topics.  Owns the
    safety-critical delete filter.
``kafka_bus``
    One consumer, one poll thread, one dispatcher greenlet, one producer per
    (bootstrap, run_id) per OS PROCESS.  All the hard parts.
``kafka_transport``
    The per-agent handle.  Nine small members over the shared bus.
``health_stream``
    The cross-process channel: each bus publishes its counters to a Kafka topic
    and the sim's finalize path reads them back, so a transport loss reaches the
    run's own outcome instead of only a worker's journal.
``factory``
    ``build_agent_transport`` -- the flag.
"""

from apps.transport.factory import build_agent_transport

__all__ = ['build_agent_transport']
