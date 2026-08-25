"""apps.dataplane.contract — the wire/data contract shared by every dataplane module.

Contains the Frame dataclass, its binary and JSON codecs, and the fixed state-code
table. This is the one subpackage every other dataplane module (hot store, duck
store, mongo archive, ingest, supervisor) imports.
"""
