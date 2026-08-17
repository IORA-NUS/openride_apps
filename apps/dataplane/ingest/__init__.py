"""Kafka ingest for the dataplane: one consumer, one group, six topics.

Modules:
  ``parse``    — payload parsing helpers copied from the retiring ``apps.kpi_sink``.
  ``consumer`` — :class:`~apps.dataplane.ingest.consumer.DataplaneConsumer`.

No re-exports here on purpose: every subpackage imports from the leaf module directly, which
keeps the import graph acyclic once ``store``/``archive``/``service`` all land.
"""
