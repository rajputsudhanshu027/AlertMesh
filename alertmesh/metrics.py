"""Privacy-safe operational counters shared by the API and ingest processes."""

from __future__ import annotations

import sqlite3
import threading


METRIC_NAMES = frozenset({
    "ingestion.attempts",
    "ingestion.successes",
    "ingestion.failures",
    "ingestion.alerts_received",
    "ingestion.alerts_processed",
    "ingestion.retries",
    "ingestion.retry_successes",
    "ingestion.duplicates_suppressed",
    "alert_processing_ms_total",
    "alert_processing_count",
    "delivery.attempts",
    "delivery.sent",
    "delivery.failed",
    "delivery.retries",
    "delivery.retry_successes",
})


class OperationalMetrics:
    """Store fixed-name aggregate counters; never store Person or Place identifiers."""

    def __init__(self, database: str) -> None:
        self.database = database
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(database, timeout=10, check_same_thread=False)
        self._connection.execute("PRAGMA busy_timeout = 10000")
        self._connection.execute("CREATE TABLE IF NOT EXISTS operational_metrics (name TEXT PRIMARY KEY, value REAL NOT NULL)")
        self._connection.commit()

    def increment(self, name: str, amount: int | float = 1) -> None:
        if name not in METRIC_NAMES:
            raise ValueError("Unknown operational metric")
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO operational_metrics(name,value) VALUES(?,?) "
                "ON CONFLICT(name) DO UPDATE SET value=value+excluded.value",
                (name, amount),
            )

    def snapshot(self) -> dict[str, int | float]:
        with self._lock:
            values = dict(self._connection.execute("SELECT name,value FROM operational_metrics"))
        return {name: int(values.get(name, 0)) if name not in {"alert_processing_ms_total"} else values.get(name, 0.0)
                for name in sorted(METRIC_NAMES)}

    def close(self) -> None:
        with self._lock:
            self._connection.close()
