"""apps.dataplane — unified Kafka-to-store dataplane for OpenRide container_logistics.

Replaces the six separate sink processes with one supervised process built on a
shared wire/frame contract (see apps.dataplane.contract.frame).
"""
