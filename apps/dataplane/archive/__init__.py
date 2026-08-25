"""apps.dataplane.archive — durable Mongo record for finished runs.

Mongo is the archive of record: DuckDB is a working set that evicts freely, and a
past run is rehydrated from here. The document shape is one document per FRAME with
the columns stored as ``bson.Binary`` (one document per position measured 20x slower
to write and 10x slower to read).

No re-exports live here on purpose — import ``apps.dataplane.archive.mongo`` directly.
"""
