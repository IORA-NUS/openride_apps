import json
import logging

from confluent_kafka.admin import AdminClient, NewTopic
from confluent_kafka import Producer
from apps.config import kafka_config

admin = None
producer = None


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


def _get_trip_geo_producer(config=None):
    global trip_geo_producer
    if trip_geo_producer is None:
        trip_geo_producer = Producer(_kafka_producer_conf(config, producer_key='producer_trip_geo'))
    return trip_geo_producer


def trip_geo_topic_name(config=None):
    selected = config or kafka_config
    return selected.get('topics', {}).get('trip_geo', 'trip_geo_stream')

def create_topic_if_not_exists(topic_name, num_partitions=2, replication_factor=1, config=None):
    admin_client = _get_admin(kafka_config)
    topic = NewTopic(topic_name, num_partitions=num_partitions, replication_factor=replication_factor, config=config)
    futures = admin_client.create_topics([topic])
    for created_topic, future in futures.items():
        try:
            future.result()
            logging.info("Topic '%s' created successfully.", created_topic)
        except Exception as e:
            if "already exists" in str(e).lower():
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


def push_event(topic_name, payload, key=None):
    producer_client = _get_producer(kafka_config)
    producer_client.poll(0)
    producer_client.produce(
        topic_name,
        key=key,
        value=json.dumps(payload),
        on_delivery=_on_delivery,
    )
    producer_client.poll(0)

def flush_producer(timeout=5):
    if producer is None:
        return 0
    return producer.flush(timeout)


def flush_trip_geo_producer(timeout=1):
    if trip_geo_producer is None:
        return 0
    return trip_geo_producer.flush(timeout)

def push_kpi_to_topic(run_id, kpi_data):
    validate_kpi_payload(kpi_data)
    push_event("kpi_stream", payload=kpi_data, key=f"{run_id}")


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
        key=f"{run_id}",
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

        create_topic_if_not_exists(
            topic_name=topic_name,
            num_partitions=spec.get('partitions', 10),
            replication_factor=spec.get('replication_factor', 1),
            config=spec.get('config', {}),
        )
