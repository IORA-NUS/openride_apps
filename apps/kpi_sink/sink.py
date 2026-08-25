"""Kafka kpi_stream + run_status consumer → DuckDB → Mongo export."""

from __future__ import annotations

import json
import logging
import signal
import threading
import time
from typing import Any, Optional

from confluent_kafka import Consumer, KafkaError

from apps.config import kafka_config, kpi_sink_settings
from apps.kpi_sink.duckdb_store import DuckDbKpiStore
from apps.kpi_sink.kpi_parse import coerce_metric_value, parse_sim_clock
from apps.kpi_sink.mongo_export import MongoKpiExporter
from apps.kpi_sink.run_status import is_simulation_lifecycle_message, should_export_run_status
from apps.utils.kafka_utils import resolve_topic

logger = logging.getLogger(__name__)


class KpiDuckDbSink:
    def __init__(self) -> None:
        self.store = DuckDbKpiStore(
            kpi_sink_settings["data_dir"],
            batch_max_rows=int(kpi_sink_settings["batch_max_rows"]),
        )
        self.exporter = MongoKpiExporter(self.store)
        self._stop = threading.Event()
        self.batch_ms = int(kpi_sink_settings["batch_ms"])
        self.group_id = kpi_sink_settings["consumer_group"]
        self.bootstrap = kafka_config["bootstrap_servers"]
        self.kpi_topic = resolve_topic("kpi")
        self.run_status_topic = resolve_topic("run_status")
        self.kpi_from_beginning = bool(kpi_sink_settings.get("from_beginning", False))
        # Phase 2: breakdown stream → DuckDB + in-process read API.
        self.breakdown_topic = resolve_topic("kpi_breakdown")
        self.breakdown_group = kpi_sink_settings.get("breakdown_group", "kpi-duckdb-breakdown")
        self.breakdown_from_beginning = bool(kpi_sink_settings.get("breakdown_from_beginning", True))
        self._http_server = None

    def _consumer_config(self, group_suffix: str, auto_offset_reset: str) -> dict:
        return {
            "bootstrap.servers": self.bootstrap,
            "group.id": f"{self.group_id}-{group_suffix}",
            "auto.offset.reset": auto_offset_reset,
            "enable.auto.commit": True,
        }

    def _decode_key(self, key: Any) -> Optional[str]:
        if key is None:
            return None
        if isinstance(key, bytes):
            return key.decode("utf-8")
        return str(key)

    def _handle_kpi_payload(self, run_id: str, payload: dict) -> None:
        metric = payload.get("metric")
        if not metric:
            logger.warning("KPI message missing metric for run_id=%s: %s", run_id, payload)
            return
        value = coerce_metric_value(payload.get("value"))
        sim_clock = parse_sim_clock(payload.get("sim_clock"))
        self.store.enqueue(run_id, str(metric), value, sim_clock)

    def _handle_breakdown_payload(self, run_id: str, payload: dict) -> None:
        scope = payload.get("scope")
        if scope not in ("truck", "haulier"):
            return  # lane scope stays on Mongo (separate /api/lanes feature)
        raw_clock = payload.get("sim_clock")
        if not raw_clock:
            return
        try:
            sim_clock = parse_sim_clock(raw_clock)
        except Exception as exc:
            logger.warning("breakdown bad sim_clock run_id=%s: %s", run_id, exc)
            return
        entities = ((payload.get("breakdown") or {}).get("entities")) or []
        if not isinstance(entities, list):
            return
        self.store.write_breakdown(run_id, str(scope), sim_clock, bool(payload.get("final")), entities)

    def _run_breakdown_consumer(self) -> None:
        consumer = Consumer(
            self._consumer_config(
                "breakdown", "earliest" if self.breakdown_from_beginning else "latest"
            )
        )
        consumer.subscribe([self.breakdown_topic])
        logger.info(
            "breakdown consumer started topic=%s group=%s-breakdown from_beginning=%s",
            self.breakdown_topic,
            self.group_id,
            self.breakdown_from_beginning,
        )
        try:
            while not self._stop.is_set():
                msg = consumer.poll(0.5)
                if msg is None:
                    continue
                if msg.error():
                    if msg.error().code() == KafkaError._PARTITION_EOF:
                        continue
                    logger.error("breakdown consumer error: %s", msg.error())
                    continue
                run_id = self._decode_key(msg.key())
                if not run_id or not msg.value():
                    continue
                try:
                    payload = json.loads(msg.value().decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    logger.warning("Invalid breakdown JSON for run_id=%s: %s", run_id, exc)
                    continue
                if not isinstance(payload, dict):
                    continue
                try:
                    self._handle_breakdown_payload(run_id, payload)
                except Exception:
                    logger.exception("breakdown handle failed run_id=%s", run_id)
        except Exception as exc:  # noqa: BLE001 - a narrower clause is what killed this thread
            logger.exception("breakdown consumer failed: %s", exc)
        finally:
            try:
                consumer.close()
            except Exception:
                pass

    def _handle_terminal_run(self, run_id: str, reason: str) -> None:
        logger.info(
            "Stop/terminal run_status for run_id=%s reason=%s — flushing and exporting",
            run_id,
            reason,
        )
        self.store.flush(run_id)
        result = self.exporter.export_run(run_id, reason=reason)
        if result.get("error"):
            logger.error("Export failed for run_id=%s: %s", run_id, result["error"])

    def _run_kpi_consumer(self) -> None:
        consumer = Consumer(
            self._consumer_config("kpi", "earliest" if self.kpi_from_beginning else "latest")
        )
        consumer.subscribe([self.kpi_topic])
        logger.info(
            "KPI consumer started topic=%s group=%s-%s from_beginning=%s",
            self.kpi_topic,
            self.group_id,
            "kpi",
            self.kpi_from_beginning,
        )
        try:
            while not self._stop.is_set():
                msg = consumer.poll(0.5)
                if msg is None:
                    continue
                if msg.error():
                    if msg.error().code() == KafkaError._PARTITION_EOF:
                        continue
                    logger.error("KPI consumer error: %s", msg.error())
                    continue
                run_id = self._decode_key(msg.key())
                if not run_id or not msg.value():
                    continue
                try:
                    payload = json.loads(msg.value().decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    logger.warning("Invalid KPI JSON for run_id=%s: %s", run_id, exc)
                    continue
                if not isinstance(payload, dict):
                    continue
                self._handle_kpi_payload(run_id, payload)
        except Exception as exc:  # noqa: BLE001 - a narrower clause is what killed this thread
            logger.exception("KPI consumer failed: %s", exc)
        finally:
            try:
                consumer.close()
            except Exception:
                pass

    def _run_status_consumer(self) -> None:
        consumer = Consumer(self._consumer_config("run_status", "latest"))
        consumer.subscribe([self.run_status_topic])
        logger.info(
            "run_status consumer started topic=%s group=%s-%s",
            self.run_status_topic,
            self.group_id,
            "run_status",
        )
        try:
            while not self._stop.is_set():
                msg = consumer.poll(0.5)
                if msg is None:
                    continue
                if msg.error():
                    if msg.error().code() == KafkaError._PARTITION_EOF:
                        continue
                    logger.error("run_status consumer error: %s", msg.error())
                    continue
                run_id = self._decode_key(msg.key())
                if not run_id or not msg.value():
                    continue
                try:
                    payload = json.loads(msg.value().decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    logger.warning("Invalid run_status JSON for run_id=%s: %s", run_id, exc)
                    continue
                if not is_simulation_lifecycle_message(payload):
                    continue
                reason = should_export_run_status(payload)
                if reason is None:
                    continue
                self._handle_terminal_run(run_id, reason)
        except Exception as exc:  # noqa: BLE001 - a narrower clause is what killed this thread
            logger.exception("run_status consumer failed: %s", exc)
        finally:
            try:
                consumer.close()
            except Exception:
                pass

    def _run_flush_timer(self) -> None:
        while not self._stop.wait(self.batch_ms / 1000.0):
            try:
                written = self.store.flush()
                if written:
                    logger.debug("Timer flush wrote %d KPI row(s)", written)
            except Exception:
                logger.exception("Timer flush failed")

    def run(self) -> None:
        def _shutdown(signum: int, _frame: Any) -> None:
            logger.info("Received signal %s — shutting down", signum)
            self._stop.set()

        signal.signal(signal.SIGINT, _shutdown)
        signal.signal(signal.SIGTERM, _shutdown)

        threads = [
            threading.Thread(target=self._run_kpi_consumer, name="kpi-consumer", daemon=True),
            threading.Thread(target=self._run_status_consumer, name="run-status-consumer", daemon=True),
            threading.Thread(target=self._run_flush_timer, name="kpi-flush-timer", daemon=True),
            threading.Thread(target=self._run_breakdown_consumer, name="breakdown-consumer", daemon=True),
        ]
        for thread in threads:
            thread.start()

        # Phase 2: in-process HTTP read API over the breakdown store (BREAKDOWN_SOURCE=duck).
        self._start_http_server()

        logger.info(
            "kpi-duckdb-sink running (data_dir=%s, batch_ms=%s)",
            kpi_sink_settings["data_dir"],
            self.batch_ms,
        )

        try:
            while not self._stop.is_set():
                time.sleep(0.5)
        finally:
            self._stop_http_server()
            self._export_all_known_runs(reason="sink_shutdown")
            self.store.flush()
            self.store.close()
            for thread in threads:
                thread.join(timeout=5)
            logger.info("kpi-duckdb-sink stopped")

    def _start_http_server(self) -> None:
        if not bool(kpi_sink_settings.get("http_enabled", True)):
            return
        try:
            from apps.kpi_sink.breakdown_api import make_breakdown_http_server

            host = kpi_sink_settings.get("http_host", "127.0.0.1")
            port = int(kpi_sink_settings.get("http_port", 8615))
            self._http_server = make_breakdown_http_server(self.store, host, port)
            threading.Thread(
                target=self._http_server.serve_forever,
                name="breakdown-http",
                daemon=True,
            ).start()
            logger.info("breakdown read API listening on http://%s:%s", host, port)
        except Exception:
            logger.exception("Failed to start breakdown HTTP server (read API disabled)")
            self._http_server = None

    def _stop_http_server(self) -> None:
        if self._http_server is not None:
            try:
                self._http_server.shutdown()
                self._http_server.server_close()
            except Exception:
                pass
            self._http_server = None

    def _export_all_known_runs(self, *, reason: str) -> None:
        for run_id in self.store.list_run_ids():
            try:
                self.store.flush(run_id)
                self.exporter.export_run(run_id, reason=reason)
            except Exception:
                logger.exception("Shutdown export failed for run_id=%s", run_id)
