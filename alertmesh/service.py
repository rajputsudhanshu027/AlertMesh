"""Application-level alert matching, inbox, and delivery flow.

Adapters are deliberately small so production providers and deterministic fakes
can use the same externally observable application behavior.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from math import asin, cos, radians, sin, sqrt
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
    instructions: str = ""
    # NWS-style geometry as GeoJSON coordinates; use point geometry for quakes.
    geometry: dict | None = None
    latitude: float | None = None
    longitude: float | None = None


@dataclass
class InboxEntry:
    person_id: str
    alert_id: str
    place_ids: set[str] = field(default_factory=set)
    updated_at: datetime = field(default_factory=utcnow)


class EmailAdapter(Protocol):
    def send(self, to: str, subject: str, body: str) -> None: ...


class AlertMesh:
    """In-memory reference application flow; inject an email adapter at runtime."""

    def __init__(self, email: EmailAdapter, *, history_days: int = 90) -> None:
        self.email = email
        self.history_days = history_days
        self.people: dict[str, Person] = {}
        self.places: dict[str, Place] = {}
        self.alerts: dict[str, Alert] = {}
        self.inbox: dict[tuple[str, str], InboxEntry] = {}
        self.delivery_status: dict[tuple[str, str], str] = {}
        self._emailed_revision: dict[tuple[str, str], tuple] = {}
        self.last_checked: dict[str, datetime] = {}
        self.source_stale: dict[str, bool] = {}

    def add_person(self, person: Person) -> None:
        self.people[person.id] = person

    def verify_person(self, person_id: str) -> None:
        self.people[person_id].verified = True

    def add_place(self, place: Place, active_alerts: list[Alert] | None = None) -> None:
        if place.person_id not in self.people:
            raise KeyError("Person does not exist")
        self.places[place.id] = place
        # Catch-up creates inbox entries without backdated email.
        for alert in active_alerts or []:
            self.ingest(alert, checked_at=place.added_at, notify=False)

    def rename_place(self, place_id: str, name: str) -> None:
        self.places[place_id].name = name

    def delete_place(self, place_id: str) -> None:
        self.places.pop(place_id, None)
        for entry in self.inbox.values():
            entry.place_ids.discard(place_id)

    def delete_person(self, person_id: str) -> None:
        self.people.pop(person_id, None)
        place_ids = [p.id for p in self.places.values() if p.person_id == person_id]
        for place_id in place_ids:
            self.delete_place(place_id)
        for key in [key for key in self.inbox if key[0] == person_id]:
            self.inbox.pop(key, None)
            self.delivery_status.pop(key, None)
            self._emailed_revision.pop(key, None)

    def ingest(self, alert: Alert, *, checked_at: datetime | None = None, notify: bool = True) -> None:
        previous = self.alerts.get(alert.id)
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
        for key, status in list(self.delivery_status.items()):
            if status == "failed" and key in self.inbox and key[0] in self.people:
                alert = self.alerts.get(key[1])
                if alert:
                    self._deliver(self.people[key[0]], alert, key, "Alert update" if key in self._emailed_revision else "New alert")

    def mark_source_stale(self, source: str) -> None:
        self.source_stale[source] = True

    def source_status(self, source: str) -> dict:
        return {"source": source, "stale": self.source_stale.get(source, True), "last_checked": self.last_checked.get(source)}

    def inbox_for(self, person_id: str, *, now: datetime | None = None) -> list[InboxEntry]:
        cutoff = (now or utcnow()) - timedelta(days=self.history_days)
        return sorted((e for e in self.inbox.values() if e.person_id == person_id and e.updated_at >= cutoff), key=lambda e: e.updated_at, reverse=True)

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
            return place.weather_enabled and (cls._point_in_geometry(place.latitude, place.longitude, alert.geometry) if alert.geometry else False)
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
