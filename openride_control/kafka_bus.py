"""Kafka publish/subscribe for service control plane."""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any, Callable

log = logging.getLogger(__name__)

# Allow importing openride_apps config when agent runs from workspace root.
_APPS_ROOT = os.path.join(os.path.dirname(__file__), "..", "openride_apps")
if _APPS_ROOT not in sys.path:
    sys.path.insert(0, os.path.normpath(_APPS_ROOT))


def _topic_names() -> tuple[str, str, str]:
    try:
        from apps.config import kafka_config

        topics = kafka_config.get("topics", {})
        return (
            topics.get("service_control", "service_control"),
            topics.get("service_status", "service_status"),
            topics.get("service_events", "service_events"),
        )
    except ImportError:
        return (
            os.getenv("KAFKA_TOPIC_SERVICE_CONTROL", "service_control"),
            os.getenv("KAFKA_TOPIC_SERVICE_STATUS", "service_status"),
            os.getenv("KAFKA_TOPIC_SERVICE_EVENTS", "service_events"),
        )


def _bootstrap_servers() -> str:
    try:
        from apps.config import kafka_config

        return kafka_config["bootstrap_servers"]
    except ImportError:
        return os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9094")


def ensure_control_topics() -> None:
    try:
        from apps.utils import kafka_utils

        kafka_utils.initialize_kafka_topics()
    except Exception as ex:
        log.warning("Could not bootstrap Kafka topics via kafka_utils: %s", ex)


def _producer():
    from confluent_kafka import Producer

    return Producer({"bootstrap.servers": _bootstrap_servers()})


def _delivery(err, msg) -> None:
    if err:
        log.error("Kafka delivery failed %s: %s", msg.topic() if msg else "?", err)


def publish_status(snapshot_dict: dict[str, Any]) -> None:
    control_topic, status_topic, _ = _topic_names()
    _ = control_topic
    payload = {
        **snapshot_dict,
        "updatedAt": datetime.now(timezone.utc).isoformat(),
    }
    key = snapshot_dict.get("key", "")
    try:
        producer = _producer()
        producer.produce(
            status_topic,
            key=key.encode("utf-8") if key else None,
            value=json.dumps(payload).encode("utf-8"),
            callback=_delivery,
        )
        producer.poll(0)
        producer.flush(5)
    except Exception as ex:
        log.warning("Failed to publish service status for %s: %s", key, ex)


def publish_event(event_dict: dict[str, Any]) -> None:
    _, _, events_topic = _topic_names()
    command_id = event_dict.get("commandId", "")
    try:
        producer = _producer()
        producer.produce(
            events_topic,
            key=command_id.encode("utf-8") if command_id else None,
            value=json.dumps(
                {**event_dict, "updatedAt": datetime.now(timezone.utc).isoformat()}
            ).encode("utf-8"),
            callback=_delivery,
        )
        producer.poll(0)
        producer.flush(5)
    except Exception as ex:
        log.warning("Failed to publish service event: %s", ex)


def publish_all_status(snapshots: list[dict[str, Any]]) -> None:
    for snap in snapshots:
        publish_status(snap)


def consume_control_commands(handler: Callable[[dict[str, Any]], None], *, poll_timeout: float = 1.0) -> None:
    from confluent_kafka import Consumer

    control_topic, _, _ = _topic_names()
    brokers = _bootstrap_servers()
    group = os.getenv("OPENRIDE_CONTROL_GROUP", "openride-control-agent")

    consumer = Consumer(
        {
            "bootstrap.servers": brokers,
            "group.id": group,
            "auto.offset.reset": "latest",
            "enable.auto.commit": True,
        }
    )
    consumer.subscribe([control_topic])
    log.info("Listening on %s (brokers=%s, group=%s)", control_topic, brokers, group)

    try:
        while True:
            msg = consumer.poll(poll_timeout)
            if msg is None:
                continue
            if msg.error():
                log.error("Consumer error: %s", msg.error())
                continue
            try:
                data = json.loads(msg.value().decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as ex:
                log.warning("Invalid control message: %s", ex)
                continue
            try:
                handler(data)
            except Exception:
                log.exception("Control command handler failed")
    finally:
        consumer.close()


def probe_broker(timeout_s: float = 3.0) -> bool:
    try:
        from confluent_kafka.admin import AdminClient

        client = AdminClient({"bootstrap.servers": _bootstrap_servers()})
        client.list_topics(timeout=timeout_s)
        return True
    except Exception:
        return False


def wait_for_broker(deadline_s: float = 120.0, interval_s: float = 2.0) -> bool:
    deadline = time.time() + deadline_s
    while time.time() < deadline:
        if probe_broker():
            return True
        time.sleep(interval_s)
    return False
