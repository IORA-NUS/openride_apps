import json
import logging
import time

from confluent_kafka.admin import AdminClient, NewTopic
from confluent_kafka import Producer
from apps.config import kafka_config

admin = None
producer = None


def resolve_topic(logical_key: str) -> str:
    """
    Map a logical key from apps.config.kafka_config['topics'] to the broker topic name.

    Examples: 'run_status' -> 'run_status', 'kpi' -> 'kpi_stream'.
    If logical_key is not listed, it is returned unchanged (supports passing an explicit topic name).
    """
    return kafka_config.get("topics", {}).get(logical_key, logical_key)


def _kafka_admin_conf(config=None):
    selected_config = config or kafka_config
    return {'bootstrap.servers': selected_config['bootstrap_servers']}


def _kafka_producer_conf(config=None, producer_key='producer'):
    selected_config = config or kafka_config
    base = selected_config.get('producer', {})
    if producer_key == 'producer':
        producer_cfg = base
    else:
        producer_cfg = {**base, **selected_config.get(producer_key, {})}
    kafka_conf = {
        'bootstrap.servers': selected_config['bootstrap_servers'],
        'socket.timeout.ms': 5000,
        'request.timeout.ms': 5000,
    }

    # Map application-friendly keys to confluent-kafka producer keys.
    key_map = {
        'linger_ms': 'linger.ms',
        'batch_size': 'batch.size',
        'compression_type': 'compression.type',
        'enable_idempotence': 'enable.idempotence',
        'acks': 'acks',
        'max_in_flight_requests_per_connection': 'max.in.flight.requests.per.connection',
    }
    for source_key, target_key in key_map.items():
        if source_key in producer_cfg:
            kafka_conf[target_key] = producer_cfg[source_key]
    return kafka_conf

def _get_admin(config=None):
    global admin
    if admin is None:
        admin = AdminClient(_kafka_admin_conf(config))
    return admin


def _get_producer(config=None):
    global producer
    if producer is None:
        producer = Producer(_kafka_producer_conf(config))
    return producer


trip_geo_producer = None
facility_stream_producer = None
perf_stream_producer = None


def _get_trip_geo_producer(config=None):
    global trip_geo_producer
    if trip_geo_producer is None:
        trip_geo_producer = Producer(_kafka_producer_conf(config, producer_key='producer_trip_geo'))
    return trip_geo_producer


def trip_geo_topic_name(config=None):
    selected = config or kafka_config
    return selected.get('topics', {}).get('trip_geo', 'trip_geo_stream')


def _get_facility_stream_producer(config=None):
    global facility_stream_producer
    if facility_stream_producer is None:
        facility_stream_producer = Producer(
            _kafka_producer_conf(config, producer_key='producer_facility_stream')
        )
    return facility_stream_producer


def facility_stream_topic_name(config=None):
    selected = config or kafka_config
    return selected.get('topics', {}).get('facility_stream', 'facility_stream')


def _get_perf_stream_producer(config=None):
    global perf_stream_producer
    if perf_stream_producer is None:
        perf_stream_producer = Producer(
            _kafka_producer_conf(config, producer_key='producer_perf')
        )
    return perf_stream_producer


def perf_stream_topic_name(config=None):
    selected = config or kafka_config
    return selected.get('topics', {}).get('perf', 'perf_stream')


def bootstrap_kafka_for_run(run_id: str, *, broker_probe_s: float = 5.0) -> str:
    """
    Ensure topics exist and publish RUNNING with minimal blocking.

    Uses non-idempotent producers by default (see KAFKA_ENABLE_IDEMPOTENCE) so startup
    does not wait on the transaction coordinator PID.
    """
    brokers = kafka_config["bootstrap_servers"]
    deadline = time.monotonic() + broker_probe_s
    while time.monotonic() < deadline:
        try:
            client = AdminClient(_kafka_admin_conf())
            metadata = client.list_topics(timeout=2)
            if metadata and metadata.brokers:
                break
        except Exception as exc:
            logging.debug("Kafka probe %s: %s", brokers, exc)
        time.sleep(0.2)
    else:
        logging.warning(
            "Kafka at %s did not respond within %ss; continuing (simulation will still run)",
            brokers,
            broker_probe_s,
        )

    initialize_kafka_topics()
    topic = resolve_topic("run_status")
    push_run_status(topic, run_id, "RUNNING")
    pending = flush_producer(3)
    if pending:
        logging.warning(
            "%s message(s) still pending on %s after flush; check %s",
            pending,
            topic,
            brokers,
        )
    return topic


def create_topic_if_not_exists(topic_name, num_partitions=2, replication_factor=1, config=None):
    admin_client = _get_admin(kafka_config)
    topic = NewTopic(topic_name, num_partitions=num_partitions, replication_factor=replication_factor, config=config)
    futures = admin_client.create_topics([topic])
    for created_topic, future in futures.items():
        try:
            future.result()
            logging.info("Topic '%s' created successfully.", created_topic)
        except Exception as e:
            err_l = str(e).lower()
            if (
                "already exists" in err_l
                or "topic_already_exists" in err_l
                or "duplicate" in err_l
            ):
                logging.info("Topic '%s' already exists.", created_topic)
            else:
                logging.error("Failed to create topic '%s': %s", created_topic, e)
                raise


def validate_kpi_payload(kpi_data):
    required_fields = ['metric', 'value', 'sim_clock']
    for field in required_fields:
        if field not in kpi_data:
            raise ValueError(f"Missing required field: {field}")


def _on_delivery(err, msg):
    if err is not None:
        logging.error("Failed to deliver event to topic '%s': %s", msg.topic(), err)


def kafka_record_key(key):
    """
    Normalize Kafka message keys for log compaction and consumer routing.

    String keys are UTF-8 encoded so compacted topics partition consistently and
    consumers never merge unrelated runs under a null key.
    """
    if key is None:
        return None
    if isinstance(key, (bytes, bytearray, memoryview)):
        return bytes(key)
    return str(key).encode("utf-8")


def push_event(topic_name, payload, key=None):
    producer_client = _get_producer(kafka_config)
    producer_client.poll(0)
    producer_client.produce(
        topic_name,
        key=kafka_record_key(key),
        value=json.dumps(payload),
        on_delivery=_on_delivery,
    )
    producer_client.poll(0)


def push_run_status(topic_name, run_id, status, **extra):
    """
    Publish simulation lifecycle to ``run_status`` (compact topic).

    Includes ``run_id`` inside the JSON so UIs that list messages without using
    the Kafka key still attribute status to the correct run. ``lifecycle_scope``
    distinguishes this from per-trip events on other streams (e.g. trip_geo).
    """
    payload = {
        "status": status,
        "run_id": run_id,
        "lifecycle_scope": "simulation",
        "simulation_active": status == "RUNNING",
    }
    if extra:
        payload.update(extra)
    push_event(topic_name, payload, key=run_id)

def flush_producer(timeout=5):
    if producer is None:
        return 0
    return producer.flush(timeout)


def flush_trip_geo_producer(timeout=1):
    if trip_geo_producer is None:
        return 0
    return trip_geo_producer.flush(timeout)


def flush_facility_stream_producer(timeout=1):
    if facility_stream_producer is None:
        return 0
    return facility_stream_producer.flush(timeout)


def push_kpi_to_topic(run_id, kpi_data):
    validate_kpi_payload(kpi_data)
    push_event(resolve_topic("kpi"), payload=kpi_data, key=run_id)


def push_trip_geo_to_topic(run_id, payload, config=None):
    """
    Publish trip_route / trip_end JSON to trip_geo_stream. Key = run_id (same as kpi_stream).
    Uses producer_trip_geo (linger.ms=0) for lower end-to-end latency than KPI batches.
    """
    topic = trip_geo_topic_name(config)
    client = _get_trip_geo_producer(config)
    client.poll(0)
    client.produce(
        topic,
        key=kafka_record_key(run_id),
        value=json.dumps(payload),
        on_delivery=_on_delivery,
    )
    client.poll(0)


def push_perf_to_topic(run_id, payload, config=None):
    """Publish performance instrumentation JSON to perf_stream. Key = run_id."""
    topic = perf_stream_topic_name(config)
    client = _get_perf_stream_producer(config)
    client.poll(0)
    client.produce(
        topic,
        key=kafka_record_key(run_id),
        value=json.dumps(payload),
        on_delivery=_on_delivery,
    )
    client.poll(0)


def flush_perf_producer(timeout=1):
    if perf_stream_producer is None:
        return 0
    return perf_stream_producer.flush(timeout)


def push_facility_to_topic(run_id, payload, config=None):
    """Publish facility_snapshot JSON to facility_stream. Key = run_id."""
    topic = facility_stream_topic_name(config)
    client = _get_facility_stream_producer(config)
    client.poll(0)
    client.produce(
        topic,
        key=kafka_record_key(run_id),
        value=json.dumps(payload),
        on_delivery=_on_delivery,
    )
    client.poll(0)


def initialize_kafka_topics():
    topics = kafka_config.get('topic_bootstrap')
    
    if not topics:
        logging.info("No Kafka topics specified for initialization.")
        return

    topic_mapping = kafka_config.get('topics', {})

    for topic_key, spec in topics.items():
        topic_name = topic_mapping.get(topic_key, topic_key)
        logging.info(
            "Kafka bootstrap: ensuring topic %r (logical key %r) partitions=%s rf=%s",
            topic_name,
            topic_key,
            spec.get("partitions", 10),
            spec.get("replication_factor", 1),
        )

        create_topic_if_not_exists(
            topic_name=topic_name,
            num_partitions=spec.get('partitions', 10),
            replication_factor=spec.get('replication_factor', 1),
            config=spec.get('config', {}),
        )
