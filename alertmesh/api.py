"""Small dependency-free HTTP API for account and AlertMesh data management.

Run locally with ``python -m alertmesh.api``. Production deployments should put
this WSGI application behind HTTPS and configure an SMTP verification adapter.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
import secrets
import smtplib
import sqlite3
import sys
import time
import uuid
from collections import deque
from urllib.parse import parse_qs
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from http import HTTPStatus
from typing import Callable
from wsgiref.simple_server import make_server

from .service import AlertMesh, Person, Place, SEVERITY
from .geocoding import Geocoder, NominatimGeocoder, OSM_ATTRIBUTION, RADIUS_PRESETS_KM


class _DiscardEmail:
    """Avoid accidental delivery from account-management requests."""

    def send(self, to: str, subject: str, body: str) -> None:
        return None


def _now() -> datetime:
    return datetime.now(timezone.utc)


class VerificationMailer:
    """Interface used to deliver a one-time email verification token."""

    def send_verification(self, email: str, token: str) -> None:
        raise NotImplementedError


class SMTPVerificationMailer(VerificationMailer):
    def __init__(self, host: str, port: int, sender: str, *, username: str | None = None, password: str | None = None, starttls: bool = True) -> None:
        self.host, self.port, self.sender = host, port, sender
        self.username, self.password, self.starttls = username, password, starttls

    def send_verification(self, email: str, token: str) -> None:
        self.send(email, "Verify your AlertMesh email", f"Your AlertMesh verification token is: {token}\n\nIt expires in 24 hours.")

    def send(self, to: str, subject: str, body: str) -> None:
        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = self.sender
        message["To"] = to
        message.set_content(body)
        with smtplib.SMTP(self.host, self.port, timeout=10) as smtp:
            if self.starttls:
                smtp.starttls()
            if self.username:
                smtp.login(self.username, self.password or "")
            smtp.send_message(message)


class AlertMeshAPI:
    """WSGI API with durable account, place, session, and inbox storage."""

    def __init__(self, database: str | None = None, mailer: VerificationMailer | None = None, *, clock: Callable[[], datetime] = _now, geocoder: Geocoder | None = None) -> None:
        self.database = database or os.environ.get("ALERTMESH_DB_PATH", "alertmesh.sqlite3")
        self.mailer = mailer
        self.clock = clock
        self.geocoder = geocoder or NominatimGeocoder()
        self._started_at = time.monotonic()
        self._api_latencies_ms = deque(maxlen=1000)
        self.flow = AlertMesh(_DiscardEmail(), db_path=database)
        self._initialize()
        self._sync_domain()

    def close(self) -> None:
        self.flow.close()

    def __call__(self, environ, start_response):
        started = time.perf_counter()
        method = environ.get("REQUEST_METHOD", "GET").upper()
        path = environ.get("PATH_INFO", "/")
        try:
            body = self._read_json(environ)
            response, status = self._dispatch(method, path, environ, body)
        except APIError as error:
            response, status = {"error": error.message}, error.status
        except (ValueError, TypeError, json.JSONDecodeError) as error:
            response, status = {"error": str(error) or "Invalid request"}, 400
        except Exception:
            response, status = {"error": "Internal server error"}, 500
        self._api_latencies_ms.append((time.perf_counter() - started) * 1000)
        encoded = json.dumps(response, separators=(",", ":")).encode("utf-8")
        start_response(f"{status} {HTTPStatus(status).phrase}", [("Content-Type", "application/json; charset=utf-8"), ("Content-Length", str(len(encoded))), ("Cache-Control", "no-store")])
        return [encoded]

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.database, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self):
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS api_people (
                    id TEXT PRIMARY KEY, email TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    password_hash BLOB NOT NULL, password_salt BLOB NOT NULL,
                    verified_at TEXT, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS api_verification_tokens (
                    token_hash BLOB PRIMARY KEY, person_id TEXT NOT NULL REFERENCES api_people(id) ON DELETE CASCADE,
                    expires_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS api_sessions (
                    token_hash BLOB PRIMARY KEY, person_id TEXT NOT NULL REFERENCES api_people(id) ON DELETE CASCADE,
                    expires_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS api_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """)

    def _sync_domain(self):
        """Import account rows created by the earlier standalone API schema."""
        with self._connect() as db:
            people = db.execute("SELECT id,email,verified_at,created_at FROM api_people").fetchall()
            synced = db.execute("SELECT 1 FROM api_meta WHERE key='legacy_domain_sync_v1'").fetchone()
            legacy_places = not synced and db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='api_places'").fetchone()
            places = db.execute("SELECT * FROM api_places").fetchall() if legacy_places else []
        self.flow.refresh()
        for row in people:
            current = self.flow.people.get(row["id"])
            person = Person(row["id"], row["email"], row["verified_at"] is not None)
            if current != person:
                self.flow.add_person(person)
        self.flow.refresh()
        for row in places:
            if row["id"] not in self.flow.places:
                self.flow.add_place(Place(
                    id=row["id"], person_id=row["person_id"], name=row["name"],
                    latitude=row["latitude"], longitude=row["longitude"],
                    radius_km=row["radius_km"], weather_enabled=bool(row["weather_enabled"]),
                    earthquake_enabled=bool(row["earthquake_enabled"]),
                    minimum_severity=row["minimum_severity"],
                    added_at=datetime.fromisoformat(row["created_at"]),
                ))
        if not synced:
            with self._connect() as db:
                db.execute("INSERT OR IGNORE INTO api_meta(key,value) VALUES('legacy_domain_sync_v1','done')")

    @staticmethod
    def _read_json(environ) -> dict:
        if environ.get("REQUEST_METHOD", "GET").upper() in {"GET", "HEAD", "DELETE"}:
            return {}
        try:
            size = int(environ.get("CONTENT_LENGTH") or 0)
        except ValueError:
            raise APIError(400, "Invalid Content-Length")
        if size > 65536:
            raise APIError(413, "Request body is too large")
        if size == 0:
            return {}
        raw = environ["wsgi.input"].read(size)
        try:
            value = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise APIError(400, "Request body must be valid JSON")
        if not isinstance(value, dict):
            raise APIError(400, "Request body must be a JSON object")
        return value

    def _dispatch(self, method, path, environ, body):
        if path == "/api/register" and method == "POST":
            return self._register(body), 201
        if path == "/api/verify" and method == "POST":
            return self._verify(body), 200
        if path == "/api/login" and method == "POST":
            return self._login(body), 200
        if path == "/api/logout" and method == "POST":
            person_id, token_hash = self._authenticate(environ)
            with self._connect() as db:
                db.execute("DELETE FROM api_sessions WHERE token_hash=?", (token_hash,))
            return {"logged_out": True}, 200
        person_id, _ = self._authenticate(environ)
        if path == "/api/geocode" and method == "GET":
            query = parse_qs(environ.get("QUERY_STRING", "")).get("q", [""])[0].strip()
            if not query or len(query) > 200:
                raise APIError(400, "q must contain between 1 and 200 characters")
            try:
                results = self.geocoder.search(query)
            except Exception:
                raise APIError(503, "Geocoding service is unavailable")
            allowed_results = [place for place in results if place.is_contiguous_us]
            return {"results": [self._geocode_json(place) for place in allowed_results], "attribution": OSM_ATTRIBUTION}, 200
        if path == "/api/me" and method == "GET":
            return self._person(person_id), 200
        if path == "/api/me" and method == "DELETE":
            with self._connect() as db:
                person = db.execute("SELECT * FROM api_people WHERE id=?", (person_id,)).fetchone()
                if person is None:
                    raise APIError(404, "Account not found")
                verification_tokens = db.execute("SELECT * FROM api_verification_tokens WHERE person_id=?", (person_id,)).fetchall()
                sessions = db.execute("SELECT * FROM api_sessions WHERE person_id=?", (person_id,)).fetchall()
                db.execute("DELETE FROM api_people WHERE id=?", (person_id,))
            try:
                self.flow.delete_person(person_id)
            except Exception:
                with self._connect() as db:
                    db.execute("INSERT INTO api_people VALUES(?,?,?,?,?,?)", tuple(person))
                    db.executemany("INSERT INTO api_verification_tokens VALUES(?,?,?)", (tuple(row) for row in verification_tokens))
                    db.executemany("INSERT INTO api_sessions VALUES(?,?,?)", (tuple(row) for row in sessions))
                raise APIError(503, "Could not delete account; please try again")
            return {"deleted": True}, 200
        if path == "/api/places" and method == "GET":
            return {"places": self._places(person_id)}, 200
        if path == "/api/places" and method == "POST":
            return self._create_place(person_id, body), 201
        if path.startswith("/api/places/"):
            place_id = path.removeprefix("/api/places/")
            if not place_id or "/" in place_id:
                raise APIError(404, "Route not found")
            if method == "PATCH":
                return self._update_place(person_id, place_id, body), 200
            if method == "DELETE":
                self.flow.refresh()
                place = self.flow.places.get(place_id)
                if place is None or place.person_id != person_id:
                    raise APIError(404, "Place not found")
                self.flow.delete_place(place_id)
                return {"deleted": True}, 200
        if path == "/api/inbox" and method == "GET":
            return self._inbox(person_id), 200
        if path == "/api/metrics" and method == "GET":
            return self._metrics(), 200
        raise APIError(404, "Route not found")

    def _register(self, body):
        email = self._required_text(body, "email").strip().lower()
        password = self._required_text(body, "password")
        if len(email) > 254 or re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email) is None:
            raise APIError(400, "Enter a valid email address")
        if len(password) < 12 or len(password) > 1024:
            raise APIError(400, "Password must be between 12 and 1024 characters")
        if self.mailer is None:
            raise APIError(503, "Email verification is not configured")
        person_id = str(uuid.uuid4())
        raw_token = secrets.token_urlsafe(32)
        salt = secrets.token_bytes(16)
        password_hash = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
        now = self.clock()
        with self._connect() as db:
            try:
                db.execute("INSERT INTO api_people(id,email,password_hash,password_salt,created_at) VALUES(?,?,?,?,?)", (person_id, email, password_hash, salt, now.isoformat()))
                db.execute("INSERT INTO api_verification_tokens(token_hash,person_id,expires_at) VALUES(?,?,?)", (self._token_hash(raw_token), person_id, (now + timedelta(hours=24)).isoformat()))
            except sqlite3.IntegrityError:
                raise APIError(409, "An account with this email already exists")
        try:
            self.flow.add_person(Person(person_id, email))
        except Exception:
            with self._connect() as db:
                db.execute("DELETE FROM api_people WHERE id=?", (person_id,))
            raise APIError(503, "Could not create account; please try again")
        try:
            self.mailer.send_verification(email, raw_token)
        except Exception:
            with self._connect() as db:
                db.execute("DELETE FROM api_people WHERE id=?", (person_id,))
            self.flow.delete_person(person_id)
            raise APIError(503, "Could not send verification email; please try again")
        return {"id": person_id, "email": email, "verified": False, "message": "Check your email for a verification token"}

    def _verify(self, body):
        raw_token = self._required_text(body, "token")
        now = self.clock()
        with self._connect() as db:
            row = db.execute("SELECT person_id,expires_at FROM api_verification_tokens WHERE token_hash=?", (self._token_hash(raw_token),)).fetchone()
            if row is None:
                raise APIError(400, "Verification token is invalid or expired")
            db.execute("DELETE FROM api_verification_tokens WHERE token_hash=?", (self._token_hash(raw_token),))
            if datetime.fromisoformat(row["expires_at"]) <= now:
                raise APIError(400, "Verification token is invalid or expired")
            db.execute("UPDATE api_people SET verified_at=? WHERE id=?", (now.isoformat(), row["person_id"]))
            person_id = row["person_id"]
            expires_at = row["expires_at"]
        try:
            self.flow.verify_person(person_id)
        except Exception:
            with self._connect() as db:
                db.execute("UPDATE api_people SET verified_at=NULL WHERE id=?", (person_id,))
                db.execute("INSERT OR REPLACE INTO api_verification_tokens(token_hash,person_id,expires_at) VALUES(?,?,?)", (self._token_hash(raw_token), person_id, expires_at))
            raise APIError(503, "Could not verify account; please try again")
        return {"verified": True}

    def _login(self, body):
        email = self._required_text(body, "email").strip().lower()
        password = self._required_text(body, "password")
        with self._connect() as db:
            row = db.execute("SELECT id,password_hash,password_salt,verified_at FROM api_people WHERE email=?", (email,)).fetchone()
            candidate = hashlib.scrypt(password.encode(), salt=row["password_salt"], n=2**14, r=8, p=1) if row else b""
            if row is None or not hmac.compare_digest(candidate, row["password_hash"]):
                raise APIError(401, "Email or password is incorrect")
            if row["verified_at"] is None:
                raise APIError(403, "Verify your email before signing in")
            token = secrets.token_urlsafe(32)
            db.execute("INSERT INTO api_sessions(token_hash,person_id,expires_at) VALUES(?,?,?)", (self._token_hash(token), row["id"], (self.clock() + timedelta(days=30)).isoformat()))
        return {"access_token": token, "token_type": "Bearer", "expires_in": 30 * 24 * 60 * 60}

    def _authenticate(self, environ):
        header = environ.get("HTTP_AUTHORIZATION", "")
        scheme, _, raw_token = header.partition(" ")
        if scheme.lower() != "bearer" or not raw_token:
            raise APIError(401, "A bearer access token is required")
        token_hash = self._token_hash(raw_token)
        with self._connect() as db:
            row = db.execute("SELECT person_id,expires_at FROM api_sessions WHERE token_hash=?", (token_hash,)).fetchone()
            if row is None:
                raise APIError(401, "Access token is invalid or expired")
            if datetime.fromisoformat(row["expires_at"]) <= self.clock():
                db.execute("DELETE FROM api_sessions WHERE token_hash=?", (token_hash,))
                raise APIError(401, "Access token is invalid or expired")
            return row["person_id"], token_hash

    def _person(self, person_id):
        self.flow.refresh()
        person = self.flow.people.get(person_id)
        if person is None:
            raise APIError(404, "Account not found")
        with self._connect() as db:
            row = db.execute("SELECT created_at FROM api_people WHERE id=?", (person_id,)).fetchone()
        if row is None:
            raise APIError(404, "Account not found")
        return {"id": person.id, "email": person.email, "verified": person.verified, "created_at": row["created_at"]}

    def _places(self, person_id):
        self.flow.refresh()
        places = sorted((place for place in self.flow.places.values() if place.person_id == person_id), key=lambda place: place.added_at)
        return [self._place_json(place) for place in places]

    def _create_place(self, person_id, body):
        data = self._validated_place(body, creating=True)
        self._require_contiguous_us(data["latitude"], data["longitude"])
        place_id = str(uuid.uuid4())
        place = Place(id=place_id, person_id=person_id, added_at=self.clock(), **data)
        try:
            self.flow.add_place(place)
        except KeyError:
            raise APIError(404, "Account not found")
        return self._place_json(place)

    def _update_place(self, person_id, place_id, body):
        self.flow.refresh()
        existing = self.flow.places.get(place_id)
        if existing is None or existing.person_id != person_id:
            raise APIError(404, "Place not found")
        current = self._place_json(existing)
        current.update(body)
        data = self._validated_place(current, creating=False, validate_radius="radius_km" in body)
        if (data["latitude"], data["longitude"]) != (existing.latitude, existing.longitude):
            self._require_contiguous_us(data["latitude"], data["longitude"])
        updated = Place(id=place_id, person_id=person_id, added_at=existing.added_at, **data)
        try:
            self.flow.update_place(updated)
        except KeyError:
            raise APIError(404, "Place not found")
        return self._place_json(updated)

    def _inbox(self, person_id):
        entries = self.flow.inbox_for(person_id, now=self.clock())
        alerts = []
        for entry in entries:
            alert = self.flow.alerts.get(entry.alert_id)
            if alert is None:
                continue
            places = [{"id": pid, "name": self.flow.places[pid].name} for pid in sorted(entry.place_ids) if pid in self.flow.places]
            alerts.append({
                "id": alert.id,
                "source": alert.source,
                "hazard": alert.hazard,
                "title": alert.title,
                "status": alert.status,
                "source_status": alert.source_status,
                "severity": alert.severity,
                "published_at": alert.published_at.isoformat(),
                "revision_at": alert.revision_at.isoformat() if alert.revision_at else None,
                "expected_end": alert.expected_end.isoformat() if alert.expected_end else None,
                "instructions": alert.instructions,
                "geometry": alert.geometry,
                "latitude": alert.latitude,
                "longitude": alert.longitude,
                "inbox_created_at": entry.updated_at.isoformat(),
                "places": places,
                "delivery": {"status": self.flow.delivery_status.get((person_id, alert.id), "not_sent")},
            })
        sources = sorted(set(self.flow.last_checked) | set(self.flow.source_stale))
        source_status = [{
            "source": source,
            "last_checked": self.flow.last_checked[source].isoformat() if source in self.flow.last_checked else None,
            "stale": self.flow.source_stale.get(source, True),
        } for source in sources]
        return {"alerts": alerts, "sources": source_status}

    def _metrics(self):
        self.flow.refresh()
        metrics = self.flow.metrics.snapshot()
        samples = sorted(self._api_latencies_ms)
        p95 = samples[math.ceil(len(samples) * 0.95) - 1] if samples else 0.0
        delivery_counts = {status: sum(value == status for value in self.flow.delivery_status.values())
                           for status in ("sent", "failed")}
        sources = sorted(set(self.flow.last_checked) | set(self.flow.source_stale))
        source_status = [{
            "source": source,
            "last_checked": self.flow.last_checked[source].isoformat() if source in self.flow.last_checked else None,
            "stale": self.flow.source_stale.get(source, True),
        } for source in sources]
        processing_count = metrics["alert_processing_count"]
        average_processing_ms = metrics["alert_processing_ms_total"] / processing_count if processing_count else 0.0
        return {
            "uptime_seconds": round(time.monotonic() - self._started_at, 3),
            "api_p95_latency_ms": round(p95, 3),
            "ingestion": {key.removeprefix("ingestion."): value for key, value in metrics.items() if key.startswith("ingestion.")},
            "alert_processing_average_ms": round(average_processing_ms, 3),
            "delivery": {
                "attempts": metrics["delivery.attempts"],
                "sent": metrics["delivery.sent"],
                "failed": metrics["delivery.failed"],
                "retries": metrics["delivery.retries"],
                "retry_successes": metrics["delivery.retry_successes"],
                "status_counts": delivery_counts,
            },
            "pending_delivery_backlog": delivery_counts["failed"],
            "sources": source_status,
        }

    @staticmethod
    def _validated_place(body, *, creating, validate_radius=True):
        fields = {"name", "latitude", "longitude", "radius_km", "weather_enabled", "earthquake_enabled", "minimum_severity"}
        required = {"name", "latitude", "longitude"}
        if creating and not required.issubset(body):
            missing = sorted(required - body.keys())
            raise APIError(400, f"Missing Place fields: {', '.join(missing)}")
        data = {key: body[key] for key in fields if key in body}
        if creating:
            data.setdefault("radius_km", 25)
            data.setdefault("weather_enabled", True)
            data.setdefault("earthquake_enabled", True)
            data.setdefault("minimum_severity", "moderate")
        try:
            data["name"] = data["name"].strip()
            data["latitude"] = float(data["latitude"])
            data["longitude"] = float(data["longitude"])
            data["radius_km"] = float(data["radius_km"])
        except (KeyError, AttributeError, TypeError, ValueError):
            raise APIError(400, "Place name, coordinates, and radius are required")
        if not data["name"] or len(data["name"]) > 80:
            raise APIError(400, "Place name must be between 1 and 80 characters")
        if not all(math.isfinite(data[key]) for key in ("latitude", "longitude", "radius_km")):
            raise APIError(400, "Coordinates and radius must be finite numbers")
        if not -90 <= data["latitude"] <= 90 or not -180 <= data["longitude"] <= 180:
            raise APIError(400, "Coordinates are outside valid latitude/longitude bounds")
        if creating or validate_radius:
            if data["radius_km"] <= 0 or data["radius_km"] > 500:
                raise APIError(400, "Radius must be greater than 0 and no more than 500 km")
            if data["radius_km"] not in RADIUS_PRESETS_KM:
                raise APIError(400, f"radius_km must be one of: {', '.join(map(str, RADIUS_PRESETS_KM))} km")
        for key in ("weather_enabled", "earthquake_enabled"):
            if not isinstance(data.get(key), bool):
                raise APIError(400, f"{key} must be a boolean")
        if data.get("minimum_severity") not in SEVERITY:
            raise APIError(400, "minimum_severity must be unknown, minor, moderate, severe, or extreme")
        return data

    def _require_contiguous_us(self, latitude, longitude):
        try:
            place = self.geocoder.reverse(latitude, longitude)
        except Exception:
            raise APIError(503, "Could not verify the Place location right now")
        if place is None or not place.is_contiguous_us:
            raise APIError(400, "Places must be in the contiguous United States")

    @staticmethod
    def _geocode_json(place):
        return {"name": place.name, "latitude": place.latitude, "longitude": place.longitude, "state_code": place.state_code}

    @staticmethod
    def _place_json(place):
        return {"id": place.id, "name": place.name, "latitude": place.latitude, "longitude": place.longitude, "radius_km": place.radius_km, "weather_enabled": place.weather_enabled, "earthquake_enabled": place.earthquake_enabled, "minimum_severity": place.minimum_severity, "created_at": place.added_at.isoformat()}

    @staticmethod
    def _required_text(body, name):
        value = body.get(name)
        if not isinstance(value, str) or not value.strip():
            raise APIError(400, f"{name} is required")
        return value

    @staticmethod
    def _token_hash(token):
        return hashlib.sha256(token.encode("utf-8")).digest()


class APIError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _from_environment():
    database = os.environ.get("ALERTMESH_DB_PATH", "alertmesh.sqlite3")
    host = os.environ.get("ALERTMESH_SMTP_HOST")
    mailer = None
    if host:
        mailer = SMTPVerificationMailer(host, int(os.environ.get("ALERTMESH_SMTP_PORT", "587")), os.environ.get("ALERTMESH_SMTP_SENDER", "alerts@localhost"), username=os.environ.get("ALERTMESH_SMTP_USERNAME"), password=os.environ.get("ALERTMESH_SMTP_PASSWORD"))
    return AlertMeshAPI(database, mailer)


if __name__ == "__main__":
    app = _from_environment()
    host, port = os.environ.get("ALERTMESH_HOST", "127.0.0.1"), int(os.environ.get("ALERTMESH_PORT", "8000"))
    print(f"AlertMesh API listening on http://{host}:{port}", file=sys.stderr)
    make_server(host, port, app).serve_forever()
