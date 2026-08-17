"""CLI entrypoint: python -m apps.kpi_sink"""

from __future__ import annotations

import logging
import sys

from apps.config import settings
from apps.kpi_sink.sink import KpiDuckDbSink


def main() -> int:
    logging.basicConfig(
        level=settings.get("LOG_LEVEL", logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    sink = KpiDuckDbSink()
    try:
        sink.run()
    except KeyboardInterrupt:
        return 0
    except Exception:
        logging.exception("kpi-duckdb-sink fatal error")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
