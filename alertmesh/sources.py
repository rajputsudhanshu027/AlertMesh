"""Official NWS and USGS source adapters and a retryable ingestion runner."""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

from .service import Alert, AlertMesh, utcnow

logger = logging.getLogger(__name__)


class SourceError(Exception):
    """A source request or response failed; non-transient by default."""

    def __init__(self, message: str, *, transient: bool = False):
        super().__init__(message)
        self.transient = transient


class TransientSourceError(SourceError):
    def __init__(self, message: str):
        super().__init__(message, transient=True)


class JsonTransport(Protocol):
    def get_json(self, url: str, *, headers: dict[str, str] | None = None) -> dict: ...


class AlertSource(Protocol):
    def fetch(self) -> list[Alert]: ...


class UrllibJsonTransport:
    """HTTP JSON client with status-aware retry classification."""

    def __init__(self, *, timeout: float = 20, user_agent: str = "AlertMesh/0.1") -> None:
        self.timeout = timeout
        self.user_agent = user_agent

    def get_json(self, url: str, *, headers: dict[str, str] | None = None) -> dict:
        request_headers = {"User-Agent": self.user_agent, "Accept": "application/geo+json, application/ld+json, application/json"}
        if headers:
            request_headers.update(headers)
        request = Request(url, headers=request_headers)
        try:
            with urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read())
        except HTTPError as error:
            retryable = error.code in {408, 429} or 500 <= error.code < 600
            error.close()
            raise SourceError(f"Source returned HTTP {error.code}", transient=retryable) from None
        except (URLError, TimeoutError, ConnectionError) as error:
            raise TransientSourceError(type(error).__name__) from None
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise SourceError("Source returned invalid JSON") from None
        if not isinstance(payload, dict):
            raise SourceError("Source response must be a JSON object")
        return payload


class SourceIdentityStore:
    """Persist aliases so provider revisions resolve to one Alert identity."""

    def __init__(self, database: str) -> None:
        self.connection = sqlite3.connect(database, timeout=10)
        self.connection.execute("PRAGMA busy_timeout = 10000")
        self.connection.execute("""
            CREATE TABLE IF NOT EXISTS source_alert_aliases (
                source TEXT NOT NULL,
                external_id TEXT NOT NULL,
                alert_id TEXT NOT NULL,
                published_at TEXT,
                PRIMARY KEY (source, external_id)
            )
        """)
        columns = {row[1] for row in self.connection.execute("PRAGMA table_info(source_alert_aliases)")}
        if "published_at" not in columns:
            self.connection.execute("ALTER TABLE source_alert_aliases ADD COLUMN published_at TEXT")
        self.connection.commit()

    def resolve(
        self,
        source: str,
        external_id: str,
        references: list[str] | None = None,
        *,
        revision_at: datetime | None = None,
        reference_published_at: datetime | None = None,
    ) -> tuple[str, datetime | None]:
        aliases = list(dict.fromkeys((references or []) + [external_id]))
        with self.connection:
            existing = self.connection.execute(
                "SELECT alert_id,published_at FROM source_alert_aliases WHERE source=? AND external_id=?",
                (source, external_id),
            ).fetchone()
            if existing:
                alert_id = existing[0]
                published_at = self._published_at(alert_id) or _parse_datetime(existing[1]) or revision_at
                self._fill_published_at(alert_id, published_at)
                return alert_id, published_at
            alert_id = None
            for alias in references or []:
                row = self.connection.execute(
                    "SELECT alert_id FROM source_alert_aliases WHERE source=? AND external_id=?",
                    (source, alias),
                ).fetchone()
                if row:
                    alert_id = row[0]
                    break
            if alert_id is None:
                root = (references or [external_id])[0]
                alert_id = f"{source}:{root}"
            published_at = self._published_at(alert_id) or reference_published_at or revision_at
            self.connection.executemany(
                "INSERT OR IGNORE INTO source_alert_aliases(source,external_id,alert_id,published_at) VALUES(?,?,?,?)",
                ((source, alias, alert_id, published_at.isoformat() if published_at else None) for alias in aliases),
            )
            self._fill_published_at(alert_id, published_at)
            return alert_id, published_at

    def _published_at(self, alert_id: str) -> datetime | None:
        row = self.connection.execute(
            "SELECT published_at FROM source_alert_aliases WHERE alert_id=? AND published_at IS NOT NULL ORDER BY rowid LIMIT 1",
            (alert_id,),
        ).fetchone()
        return _parse_datetime(row[0]) if row else None

    def _fill_published_at(self, alert_id: str, published_at: datetime | None) -> None:
        if published_at is not None:
            self.connection.execute(
                "UPDATE source_alert_aliases SET published_at=? WHERE alert_id=? AND published_at IS NULL",
                (published_at.isoformat(), alert_id),
            )

    def close(self) -> None:
        self.connection.close()


class NWSAdapter:
    """Fetch recent actual NWS alert products, including update/cancel messages."""

    ACTIVE_FEED_URL = "https://api.weather.gov/alerts/active?status=Actual&limit=500"
    RECENT_FEED_URL = "https://api.weather.gov/alerts?status=Actual&limit=500"
    SOURCE = "nws"

    def __init__(self, transport: JsonTransport, identities: SourceIdentityStore, *, clock: Callable[[], datetime] = utcnow, max_pages: int = 50) -> None:
        self.transport = transport
        self.identities = identities
        self.clock = clock
        self.max_pages = max_pages

    def fetch(self) -> list[Alert]:
        features = self._fetch_pages(self.ACTIVE_FEED_URL)
        features.extend(self._fetch_pages(self.RECENT_FEED_URL))
        alerts: dict[str, Alert] = {}
        for feature in features:
            alert = self.normalize_feature(feature)
            previous = alerts.get(alert.id)
            if previous is None or alert.published_at >= previous.published_at:
                alerts[alert.id] = alert
        return list(alerts.values())

    def _fetch_pages(self, initial_url: str) -> list[dict]:
        url = initial_url
        visited: set[str] = set()
        features_seen: list[dict] = []
        for _ in range(self.max_pages):
            if url in visited:
                raise SourceError("NWS pagination loop detected")
            visited.add(url)
            payload = self.transport.get_json(url, headers={"Accept": "application/geo+json"})
            features = payload.get("features")
            if not isinstance(features, list):
                raise SourceError("NWS response has no feature list")
            features_seen.extend(features)
            next_url = (payload.get("pagination") or {}).get("next")
            if not next_url:
                return features_seen
            absolute_next = urljoin(url, next_url)
            parsed = urlparse(absolute_next)
            if parsed.scheme != "https" or parsed.hostname != "api.weather.gov":
                raise SourceError("NWS returned an unexpected pagination URL")
            url = absolute_next
        raise SourceError("NWS pagination exceeded the page limit")

    def normalize_feature(self, feature: dict) -> Alert:
        if not isinstance(feature, dict) or not isinstance(feature.get("properties"), dict):
            raise SourceError("NWS feature is malformed")
        properties = feature["properties"]
        external_id = properties.get("id") or properties.get("identifier") or feature.get("id")
        if not isinstance(external_id, str) or not external_id:
            raise SourceError("NWS alert has no source identifier")
        references, reference_published_at = _nws_reference_info(properties.get("references"))
        revision_at = _parse_datetime(properties.get("sent") or properties.get("effective") or properties.get("onset"))
        if revision_at is None:
            raise SourceError("NWS alert has no publication time")
        alert_id, published = self.identities.resolve(
            self.SOURCE,
            external_id,
            references,
            revision_at=revision_at,
            reference_published_at=reference_published_at,
        )
        expected_end = _parse_datetime(properties.get("ends") or properties.get("expires"))
        message_type = str(properties.get("messageType") or "Alert").lower()
        now = self.clock()
        if message_type == "cancel":
            status = "cancelled"
            ended_at = now
        elif expected_end is not None and expected_end <= now:
            status = "expired"
            ended_at = expected_end
        else:
            status = "active"
            ended_at = None
        severity = str(properties.get("severity") or "unknown").lower()
        if severity not in {"unknown", "minor", "moderate", "severe", "extreme"}:
            severity = "unknown"
        return Alert(
            id=alert_id,
            source=self.SOURCE,
            hazard="weather",
            title=str(properties.get("headline") or properties.get("event") or "Weather alert"),
            published_at=published or revision_at,
            revision_at=revision_at,
            status=status,
            source_status=str(properties.get("status") or "").lower() or None,
            severity=severity,
            expected_end=expected_end,
            ended_at=ended_at,
            instructions=str(properties.get("instruction") or ""),
            geometry=feature.get("geometry"),
        )


class USGSAdapter:
    """Fetch all earthquakes in the current USGS 7-day GeoJSON feed."""

    FEED_URL = "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/all_week.geojson"
    SOURCE = "usgs"

    def __init__(self, transport: JsonTransport, *, clock: Callable[[], datetime] = utcnow) -> None:
        self.transport = transport
        self.clock = clock

    def fetch(self) -> list[Alert]:
        payload = self.transport.get_json(self.FEED_URL)
        features = payload.get("features")
        if not isinstance(features, list):
            raise SourceError("USGS response has no feature list")
        return [self.normalize_feature(feature) for feature in features]

    def normalize_feature(self, feature: dict) -> Alert:
        if not isinstance(feature, dict) or not isinstance(feature.get("properties"), dict):
            raise SourceError("USGS feature is malformed")
        properties = feature["properties"]
        external_id = feature.get("id") or properties.get("code")
        if not isinstance(external_id, str) or not external_id:
            raise SourceError("USGS event has no source identifier")
        geometry = feature.get("geometry") or {}
        coordinates = geometry.get("coordinates")
        if geometry.get("type") != "Point" or not isinstance(coordinates, list) or len(coordinates) < 2:
            raise SourceError("USGS event has no point geometry")
        try:
            longitude, latitude = float(coordinates[0]), float(coordinates[1])
            milliseconds = float(properties["time"])
            published = datetime.fromtimestamp(milliseconds / 1000, timezone.utc)
            updated = properties.get("updated")
            revision_at = datetime.fromtimestamp(float(updated) / 1000, timezone.utc) if updated is not None else None
        except (KeyError, TypeError, ValueError, OverflowError, OSError):
            raise SourceError("USGS event has invalid origin time or coordinates") from None
        title = properties.get("title") or properties.get("place") or f"Earthquake {external_id}"
        source_status = str(properties.get("status") or "").lower() or None
        deleted = source_status == "deleted"
        return Alert(
            id=f"{self.SOURCE}:{external_id}",
            source=self.SOURCE,
            hazard="earthquake",
            title=str(title),
            published_at=published,
            status="cancelled" if deleted else "active",
            source_status=source_status,
            ended_at=self.clock() if deleted else None,
            severity="unknown",
            geometry={"type": "Point", "coordinates": [longitude, latitude]},
            longitude=longitude,
            latitude=latitude,
            revision_at=revision_at,
        )


@dataclass(frozen=True)
class IngestionResult:
    source: str
    success: bool
    attempts: int
    received: int
    ingested: int
    failed: int
    last_checked: datetime | None


class IngestionRunner:
    """Fetch feeds, retry temporary errors, and send normalized alerts to the app."""

    def __init__(
        self,
        app: AlertMesh,
        adapters: dict[str, AlertSource],
        *,
        max_attempts: int = 3,
        base_delay: float = 1,
        max_delay: float = 30,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        if max_attempts < 1 or base_delay < 0 or max_delay < 0:
            raise ValueError("Retry attempts and delays must be non-negative, with at least one attempt")
        self.app = app
        self.adapters = adapters
        self.max_attempts = max_attempts
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.sleep = sleep
        self.clock = clock

    def run_source(self, source: str) -> IngestionResult:
        adapter = self.adapters.get(source)
        if adapter is None:
            raise ValueError(f"No adapter configured for source {source}")
        alerts: list[Alert] = []
        attempts = 0
        for attempt in range(1, self.max_attempts + 1):
            attempts = attempt
            try:
                alerts = list(adapter.fetch())
                break
            except Exception as error:
                retryable = isinstance(error, (TransientSourceError, TimeoutError, ConnectionError, URLError)) or bool(getattr(error, "transient", False))
                if retryable and attempt < self.max_attempts:
                    self.sleep(min(self.base_delay * (2 ** (attempt - 1)), self.max_delay))
                    continue
                self.app.metrics.increment("ingestion.attempts", attempts)
                self.app.metrics.increment("ingestion.retries", attempts - 1)
                self.app.metrics.increment("ingestion.failures")
                self.app.mark_source_stale(source)
                logger.warning("Ingestion failed for %s after %d attempt(s)", source, attempt)
                return IngestionResult(source, False, attempt, 0, 0, 0, self.app.source_status(source)["last_checked"])
        checked_at = self.clock()
        self.app.metrics.increment("ingestion.attempts", attempts)
        self.app.metrics.increment("ingestion.retries", attempts - 1)
        if attempts > 1:
            self.app.metrics.increment("ingestion.retry_successes")
        self.app.metrics.increment("ingestion.alerts_received", len(alerts))
        self.app.mark_source_checked(source, checked_at)
        ingested = failed = 0
        for alert in alerts:
            try:
                duplicate = self.app.ingest(alert, checked_at=checked_at)
                if duplicate:
                    self.app.metrics.increment("ingestion.duplicates_suppressed")
                ingested += 1
            except Exception:
                failed += 1
                logger.error("Failed to process a normalized alert from %s", source)
        if failed:
            self.app.metrics.increment("ingestion.failures")
            self.app.mark_source_stale(source)
        else:
            self.app.metrics.increment("ingestion.successes")
            self.app.expire_missing(source, {alert.id for alert in alerts}, checked_at=checked_at)
        self.app.metrics.increment("ingestion.alerts_processed", ingested)
        return IngestionResult(source, failed == 0, attempts, len(alerts), ingested, failed, checked_at)

    def run_once(self) -> list[IngestionResult]:
        return [self.run_source(source) for source in sorted(self.adapters)]

    def run_forever(self, interval_seconds: float = 60, stop_event=None) -> None:
        if interval_seconds <= 0:
            raise ValueError("Polling interval must be positive")
        while stop_event is None or not stop_event.is_set():
            self.run_once()
            if stop_event is None:
                time.sleep(interval_seconds)
            elif stop_event.wait(interval_seconds):
                return


def _nws_reference_info(references) -> tuple[list[str], datetime | None]:
    if not isinstance(references, list):
        return [], None
    identifiers: list[str] = []
    published_at = None
    for reference in references:
        if isinstance(reference, dict):
            identifier = reference.get("id")
            published_at = published_at or _parse_datetime(reference.get("sent"))
        elif isinstance(reference, str):
            identifier = reference.split()[0] if reference.split() else None
        else:
            identifier = None
        if isinstance(identifier, str) and identifier:
            identifiers.append(identifier)
    return identifiers, published_at


def _parse_datetime(value) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
