"""Application-level alert matching, inbox, and delivery flow.

Adapters are deliberately small so production providers and deterministic fakes
can use the same externally observable application behavior.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from math import asin, cos, radians, sin, sqrt
import os
from typing import Protocol

SEVERITY = {"unknown": 0, "minor": 1, "moderate": 2, "severe": 3, "extreme": 4}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Person:
    id: str
    email: str
    verified: bool = False


@dataclass
class Place:
    id: str
    person_id: str
    name: str
    latitude: float
    longitude: float
    radius_km: float = 25
    weather_enabled: bool = True
    earthquake_enabled: bool = True
    minimum_severity: str = "moderate"
    added_at: datetime = field(default_factory=utcnow)

    def __post_init__(self) -> None:
        if not -90 <= self.latitude <= 90 or not -180 <= self.longitude <= 180:
            raise ValueError("Place coordinates are outside valid latitude/longitude bounds")
        if self.radius_km <= 0:
            raise ValueError("Place radius must be positive")
        if self.minimum_severity not in SEVERITY:
            raise ValueError("Unknown minimum weather severity")


@dataclass
class Alert:
    id: str
    source: str
    hazard: str
    title: str
    published_at: datetime
    status: str = "active"
    severity: str = "unknown"
    expected_end: datetime | None = None
    ended_at: datetime | None = None
    instructions: str = ""
    # NWS-style geometry as GeoJSON coordinates; use point geometry for quakes.
    geometry: dict | None = None
    latitude: float | None = None
    longitude: float | None = None
    revision_at: datetime | None = None
    source_status: str | None = None


@dataclass
class InboxEntry:
    person_id: str
    alert_id: str
    place_ids: set[str] = field(default_factory=set)
    updated_at: datetime = field(default_factory=utcnow)


class EmailAdapter(Protocol):
    def send(self, to: str, subject: str, body: str) -> None: ...


class AlertMesh:
    """Application flow backed by SQLite; inject email and storage at startup."""

    def __init__(self, email: EmailAdapter, *, history_days: int = 90, db_path: str | None = None) -> None:
        from .storage import SQLiteStore

        self.email = email
        self.history_days = history_days
        self._store = SQLiteStore(db_path or os.environ.get("ALERTMESH_DB_PATH", "alertmesh.sqlite3"))
        self._reload()

    def close(self) -> None:
        """Close the storage connection after the application instance is retired."""
        self._store.close()

    def _persist(self) -> None:
        self._store.save(self.people, self.places, self.alerts, self.inbox, self.delivery_status,
                         self._emailed_revision, self.last_checked, self.source_stale)

    def _reload(self) -> None:
        (self.people, self.places, self.alerts, self.inbox, self.delivery_status,
         self._emailed_revision, source_status) = self._store.load()
        self.last_checked = {source: checked for source, (checked, _) in source_status.items() if checked is not None}
        self.source_stale = {source: stale for source, (_, stale) in source_status.items()}

    def refresh(self) -> None:
        """Reload the latest committed application state from SQLite."""
        with self._store.transaction():
            self._reload()

    def add_person(self, person: Person) -> None:
        with self._store.transaction():
            self._reload()
            self.people[person.id] = person
            self._persist()

    def verify_person(self, person_id: str) -> None:
        with self._store.transaction():
            self._reload()
            self.people[person_id].verified = True
            self._persist()

    def add_place(self, place: Place, active_alerts: list[Alert] | None = None) -> None:
        with self._store.transaction():
            self._reload()
            if place.person_id not in self.people:
                raise KeyError("Person does not exist")
            self.places[place.id] = place
            # Catch-up creates inbox entries without backdated email.
            catch_up = active_alerts if active_alerts is not None else [a for a in self.alerts.values() if a.status in {"active", "unknown"}]
            for alert in catch_up:
                self._ingest_current(alert, checked_at=place.added_at, notify=False)
            self._persist()

    def rename_place(self, place_id: str, name: str) -> None:
        with self._store.transaction():
            self._reload()
            self.places[place_id].name = name
            self._persist()

    def update_place(self, place: Place) -> None:
        """Replace a saved Place while preserving its owning Person."""
        with self._store.transaction():
            self._reload()
            existing = self.places.get(place.id)
            if existing is None or existing.person_id != place.person_id:
                raise KeyError("Place does not exist")
            self.places[place.id] = place
            for alert in self.alerts.values():
                key = (place.person_id, alert.id)
                matches = [p for p in self.places.values() if p.person_id == place.person_id and self._matches(alert, p)]
                entry = self.inbox.get(key)
                if matches:
                    if entry is None:
                        entry = InboxEntry(place.person_id, alert.id)
                        self.inbox[key] = entry
                    entry.place_ids = {p.id for p in matches}
                    entry.updated_at = utcnow()
                elif entry:
                    entry.place_ids.discard(place.id)
            self._persist()

    def delete_place(self, place_id: str) -> None:
        with self._store.transaction():
            self._reload()
            self.places.pop(place_id, None)
            for entry in self.inbox.values():
                entry.place_ids.discard(place_id)
            self._persist()

    def delete_person(self, person_id: str) -> None:
        with self._store.transaction():
            self._reload()
            self.people.pop(person_id, None)
            for place_id in [p.id for p in self.places.values() if p.person_id == person_id]:
                self.places.pop(place_id, None)
                for entry in self.inbox.values():
                    entry.place_ids.discard(place_id)
            for key in [key for key in self.inbox if key[0] == person_id]:
                self.inbox.pop(key, None)
                self.delivery_status.pop(key, None)
                self._emailed_revision.pop(key, None)
            self._persist()

    def ingest(self, alert: Alert, *, checked_at: datetime | None = None, notify: bool = True) -> None:
        with self._store.transaction():
            self._reload()
            self._ingest_current(alert, checked_at=checked_at, notify=notify)
            self._persist()

    def _ingest_current(self, alert: Alert, *, checked_at: datetime | None = None, notify: bool = True) -> None:
        previous = self.alerts.get(alert.id)
        if alert.status in {"cancelled", "expired"} and alert.ended_at is None:
            alert.ended_at = checked_at or utcnow()
        self.alerts[alert.id] = alert
        self.last_checked[alert.source] = checked_at or utcnow()
        self.source_stale[alert.source] = False
        for person in list(self.people.values()):
            matches = [p for p in self.places.values() if p.person_id == person.id and self._matches(alert, p)]
            key = (person.id, alert.id)
            entry = self.inbox.get(key)
            if matches:
                if entry is None:
                    entry = InboxEntry(person.id, alert.id)
                    self.inbox[key] = entry
                entry.place_ids = {p.id for p in matches}
                entry.updated_at = checked_at or utcnow()
                if notify and person.verified and self._email_eligible(alert, matches):
                    revision = self._material_revision(alert)
                    sent_revision = self._emailed_revision.get(key)
                    should_send = sent_revision is None or (previous is not None and revision != sent_revision)
                    if should_send:
                        self._deliver(person, alert, key, "New alert" if sent_revision is None else "Alert update")
                        self._emailed_revision[key] = revision
            elif entry:
                # Preserve historical match, but remove deleted/disabled places from current view.
                entry.place_ids = {pid for pid in entry.place_ids if pid in self.places}

    def retry_failed_deliveries(self) -> None:
        with self._store.transaction():
            self._reload()
            for key, status in list(self.delivery_status.items()):
                if status == "failed" and key in self.inbox and key[0] in self.people:
                    alert = self.alerts.get(key[1])
                    if alert:
                        self._deliver(self.people[key[0]], alert, key, "Alert update" if key in self._emailed_revision else "New alert")
            self._persist()

    def mark_source_stale(self, source: str) -> None:
        with self._store.transaction():
            self._reload()
            self.source_stale[source] = True
            self._persist()

    def mark_source_checked(self, source: str, checked_at: datetime | None = None) -> None:
        """Record a successful feed check, including an empty result."""
        with self._store.transaction():
            self._reload()
            self.last_checked[source] = checked_at or utcnow()
            self.source_stale[source] = False
            self._persist()

    def expire_missing(self, source: str, present_alert_ids: set[str], *, checked_at: datetime | None = None) -> int:
        """Expire active source Alerts omitted from a complete successful snapshot."""
        ended_at = checked_at or utcnow()
        count = 0
        with self._store.transaction():
            self._reload()
            for alert in self.alerts.values():
                if alert.source != source or alert.id in present_alert_ids or alert.status not in {"active", "unknown"}:
                    continue
                alert.status = "expired"
                if alert.expected_end is not None and alert.expected_end <= ended_at:
                    alert.ended_at = alert.expected_end
                else:
                    alert.ended_at = ended_at
                count += 1
            if count:
                self._persist()
        return count

    def source_status(self, source: str) -> dict:
        self.refresh()
        return {"source": source, "stale": self.source_stale.get(source, True), "last_checked": self.last_checked.get(source)}

    def inbox_for(self, person_id: str, *, now: datetime | None = None) -> list[InboxEntry]:
        self.refresh()
        self.prune_expired(now=now)
        return sorted((e for e in self.inbox.values() if e.person_id == person_id), key=lambda e: e.updated_at, reverse=True)

    def prune_expired(self, *, now: datetime | None = None) -> int:
        """Permanently remove terminal inbox records beyond the configured retention."""
        cutoff = (now or utcnow()) - timedelta(days=self.history_days)
        expired = [
            key for key, entry in self.inbox.items()
            if self.alerts[entry.alert_id].status in {"cancelled", "expired"}
            and (self.alerts[entry.alert_id].ended_at or entry.updated_at) < cutoff
        ]
        for key in expired:
            self.inbox.pop(key, None)
            self.delivery_status.pop(key, None)
            self._emailed_revision.pop(key, None)
        if expired:
            self._persist()
        return len(expired)

    def _deliver(self, person: Person, alert: Alert, key: tuple[str, str], subject: str) -> None:
        try:
            places = ", ".join(sorted(self.places[i].name for i in self.inbox[key].place_ids if i in self.places))
            self.email.send(person.email, f"{subject}: {alert.title}", f"{alert.title}\nAffected places: {places}")
            self.delivery_status[key] = "sent"
        except Exception:
            self.delivery_status[key] = "failed"

    @staticmethod
    def _material_revision(alert: Alert) -> tuple:
        return (alert.severity, _freeze(alert.geometry), alert.instructions.strip(), alert.expected_end)

    @staticmethod
    def _email_eligible(alert: Alert, places: list[Place]) -> bool:
        if alert.hazard == "earthquake":
            return any(p.earthquake_enabled for p in places)
        if alert.hazard != "weather" or alert.severity == "unknown":
            return False
        return any(p.weather_enabled and SEVERITY[alert.severity] >= SEVERITY[p.minimum_severity] for p in places)

    @classmethod
    def _matches(cls, alert: Alert, place: Place) -> bool:
        if alert.status not in {"active", "unknown"}:
            # Keep existing inbox history, do not create a new match for a terminal alert.
            return False
        if alert.hazard == "weather":
            if not place.weather_enabled or not (cls._point_in_geometry(place.latitude, place.longitude, alert.geometry) if alert.geometry else False):
                return False
            # Unknown severity remains visible; known severity honors each place's threshold.
            return alert.severity == "unknown" or (alert.severity in SEVERITY and SEVERITY[alert.severity] >= SEVERITY[place.minimum_severity])
        if alert.hazard == "earthquake":
            return place.earthquake_enabled and alert.latitude is not None and alert.longitude is not None and _distance_km(place.latitude, place.longitude, alert.latitude, alert.longitude) <= place.radius_km
        return False

    @classmethod
    def _point_in_geometry(cls, lat: float, lon: float, geometry: dict | None) -> bool:
        if not geometry:
            return False
        kind, coords = geometry.get("type"), geometry.get("coordinates", [])
        polygons = [coords] if kind == "Polygon" else coords if kind == "MultiPolygon" else []
        return any(_point_in_polygon(lon, lat, polygon) for polygon in polygons)


def _freeze(value):
    if isinstance(value, dict):
        return tuple((k, _freeze(v)) for k, v in sorted(value.items()))
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(v) for v in value)
    return value


def _point_in_polygon(x: float, y: float, rings: list) -> bool:
    if not rings or not _in_ring(x, y, rings[0]):
        return False
    return not any(_in_ring(x, y, hole) for hole in rings[1:])


def _in_ring(x: float, y: float, ring: list) -> bool:
    inside = False
    for i, (xi, yi) in enumerate(ring):
        xj, yj = ring[i - 1]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-20) + xi:
            inside = not inside
    return inside


def _distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    dlat, dlon = radians(lat2 - lat1), radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return 6371.0088 * 2 * asin(sqrt(a))
