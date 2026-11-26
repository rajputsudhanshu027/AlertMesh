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
import secrets
import smtplib
import sqlite3
import sys
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from http import HTTPStatus
from typing import Callable
from wsgiref.simple_server import make_server

from .service import SEVERITY


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
        message = EmailMessage()
        message["Subject"] = "Verify your AlertMesh email"
        message["From"] = self.sender
        message["To"] = email
        message.set_content(f"Your AlertMesh verification token is: {token}\n\nIt expires in 24 hours.")
        with smtplib.SMTP(self.host, self.port, timeout=10) as smtp:
            if self.starttls:
                smtp.starttls()
            if self.username:
                smtp.login(self.username, self.password or "")
            smtp.send_message(message)


class AlertMeshAPI:
    """WSGI API with durable account, place, session, and inbox storage."""

    def __init__(self, database: str = "alertmesh.sqlite3", mailer: VerificationMailer | None = None, *, clock: Callable[[], datetime] = _now) -> None:
        self.database = database
        self.mailer = mailer
        self.clock = clock
        self._initialize()

    def __call__(self, environ, start_response):
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
                CREATE TABLE IF NOT EXISTS api_places (
                    id TEXT PRIMARY KEY, person_id TEXT NOT NULL REFERENCES api_people(id) ON DELETE CASCADE,
                    name TEXT NOT NULL, latitude REAL NOT NULL, longitude REAL NOT NULL,
                    radius_km REAL NOT NULL DEFAULT 25, weather_enabled INTEGER NOT NULL DEFAULT 1,
                    earthquake_enabled INTEGER NOT NULL DEFAULT 1, minimum_severity TEXT NOT NULL DEFAULT 'moderate',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS api_alerts (
                    id TEXT PRIMARY KEY, source TEXT NOT NULL, hazard TEXT NOT NULL, title TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active', severity TEXT NOT NULL DEFAULT 'unknown',
                    published_at TEXT NOT NULL, expected_end TEXT, instructions TEXT NOT NULL DEFAULT '',
                    geometry TEXT, latitude REAL, longitude REAL, ended_at TEXT
                );
                CREATE TABLE IF NOT EXISTS api_inbox_entries (
                    person_id TEXT NOT NULL REFERENCES api_people(id) ON DELETE CASCADE,
                    alert_id TEXT NOT NULL REFERENCES api_alerts(id) ON DELETE CASCADE,
                    created_at TEXT NOT NULL, PRIMARY KEY (person_id, alert_id)
                );
                CREATE TABLE IF NOT EXISTS api_inbox_places (
                    person_id TEXT NOT NULL, alert_id TEXT NOT NULL, place_id TEXT NOT NULL,
                    PRIMARY KEY (person_id, alert_id, place_id),
                    FOREIGN KEY (person_id, alert_id) REFERENCES api_inbox_entries(person_id, alert_id) ON DELETE CASCADE,
                    FOREIGN KEY (place_id) REFERENCES api_places(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS api_email_deliveries (
                    person_id TEXT NOT NULL REFERENCES api_people(id) ON DELETE CASCADE,
                    alert_id TEXT NOT NULL REFERENCES api_alerts(id) ON DELETE CASCADE,
                    status TEXT NOT NULL, attempted_at TEXT, PRIMARY KEY (person_id, alert_id)
                );
                CREATE TABLE IF NOT EXISTS api_source_status (
                    source TEXT PRIMARY KEY, last_checked TEXT, stale INTEGER NOT NULL DEFAULT 1
                );
            """)

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
        if path == "/api/me" and method == "GET":
            return self._person(person_id), 200
        if path == "/api/me" and method == "DELETE":
            with self._connect() as db:
                db.execute("DELETE FROM api_people WHERE id=?", (person_id,))
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
                with self._connect() as db:
                    cursor = db.execute("DELETE FROM api_places WHERE id=? AND person_id=?", (place_id, person_id))
                    if cursor.rowcount == 0:
                        raise APIError(404, "Place not found")
                return {"deleted": True}, 200
        if path == "/api/inbox" and method == "GET":
            return self._inbox(person_id), 200
        raise APIError(404, "Route not found")

    def _register(self, body):
        email = self._required_text(body, "email").strip().lower()
        password = self._required_text(body, "password")
        if len(email) > 254 or "@" not in email or email.startswith("@") or email.endswith("@"):
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
            self.mailer.send_verification(email, raw_token)
        except Exception:
            with self._connect() as db:
                db.execute("DELETE FROM api_people WHERE id=?", (person_id,))
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
        with self._connect() as db:
            row = db.execute("SELECT id,email,verified_at,created_at FROM api_people WHERE id=?", (person_id,)).fetchone()
        if row is None:
            raise APIError(404, "Account not found")
        return {"id": row["id"], "email": row["email"], "verified": row["verified_at"] is not None, "created_at": row["created_at"]}

    def _places(self, person_id):
        with self._connect() as db:
            rows = db.execute("SELECT * FROM api_places WHERE person_id=? ORDER BY created_at", (person_id,)).fetchall()
        return [self._place_json(row) for row in rows]

    def _create_place(self, person_id, body):
        data = self._validated_place(body, creating=True)
        place_id = str(uuid.uuid4())
        with self._connect() as db:
            db.execute("INSERT INTO api_places(id,person_id,name,latitude,longitude,radius_km,weather_enabled,earthquake_enabled,minimum_severity,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)", (place_id, person_id, data["name"], data["latitude"], data["longitude"], data["radius_km"], int(data["weather_enabled"]), int(data["earthquake_enabled"]), data["minimum_severity"], self.clock().isoformat()))
            row = db.execute("SELECT * FROM api_places WHERE id=?", (place_id,)).fetchone()
        return self._place_json(row)

    def _update_place(self, person_id, place_id, body):
        with self._connect() as db:
            row = db.execute("SELECT * FROM api_places WHERE id=? AND person_id=?", (place_id, person_id)).fetchone()
            if row is None:
                raise APIError(404, "Place not found")
            current = self._place_json(row)
            current.update(body)
            data = self._validated_place(current, creating=False)
            db.execute("UPDATE api_places SET name=?,latitude=?,longitude=?,radius_km=?,weather_enabled=?,earthquake_enabled=?,minimum_severity=? WHERE id=? AND person_id=?", (data["name"], data["latitude"], data["longitude"], data["radius_km"], int(data["weather_enabled"]), int(data["earthquake_enabled"]), data["minimum_severity"], place_id, person_id))
            updated = db.execute("SELECT * FROM api_places WHERE id=?", (place_id,)).fetchone()
        return self._place_json(updated)

    def _inbox(self, person_id):
        cutoff = (self.clock() - timedelta(days=90)).isoformat()
        with self._connect() as db:
            rows = db.execute("""
                SELECT a.*, i.created_at AS inbox_created,
                       COALESCE(d.status, 'not_sent') AS delivery_status,
                       d.attempted_at
                FROM api_inbox_entries i JOIN api_alerts a ON a.id=i.alert_id
                LEFT JOIN api_email_deliveries d ON d.person_id=i.person_id AND d.alert_id=i.alert_id
                WHERE i.person_id=? AND (a.ended_at IS NULL OR a.ended_at>=?)
                ORDER BY i.created_at DESC
            """, (person_id, cutoff)).fetchall()
            status = db.execute("SELECT source,last_checked,stale FROM api_source_status ORDER BY source").fetchall()
            alerts = []
            for row in rows:
                places = db.execute("SELECT p.id,p.name FROM api_inbox_places ip JOIN api_places p ON p.id=ip.place_id WHERE ip.person_id=? AND ip.alert_id=? ORDER BY p.name", (person_id, row["id"])).fetchall()
                alerts.append({"id": row["id"], "source": row["source"], "hazard": row["hazard"], "title": row["title"], "status": row["status"], "severity": row["severity"], "published_at": row["published_at"], "expected_end": row["expected_end"], "instructions": row["instructions"], "geometry": json.loads(row["geometry"]) if row["geometry"] else None, "latitude": row["latitude"], "longitude": row["longitude"], "inbox_created_at": row["inbox_created"], "places": [{"id": p["id"], "name": p["name"]} for p in places], "delivery": {"status": row["delivery_status"], "attempted_at": row["attempted_at"]}})
        return {"alerts": alerts, "sources": [{"source": r["source"], "last_checked": r["last_checked"], "stale": bool(r["stale"])} for r in status]}

    @staticmethod
    def _validated_place(body, *, creating):
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
        if data["radius_km"] <= 0 or data["radius_km"] > 500:
            raise APIError(400, "Radius must be greater than 0 and no more than 500 km")
        for key in ("weather_enabled", "earthquake_enabled"):
            if not isinstance(data.get(key), bool):
                raise APIError(400, f"{key} must be a boolean")
        if data.get("minimum_severity") not in SEVERITY:
            raise APIError(400, "minimum_severity must be unknown, minor, moderate, severe, or extreme")
        return data

    @staticmethod
    def _place_json(row):
        return {"id": row["id"], "name": row["name"], "latitude": row["latitude"], "longitude": row["longitude"], "radius_km": row["radius_km"], "weather_enabled": bool(row["weather_enabled"]), "earthquake_enabled": bool(row["earthquake_enabled"]), "minimum_severity": row["minimum_severity"], "created_at": row["created_at"]}

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
    database = os.environ.get("ALERTMESH_DATABASE", "alertmesh.sqlite3")
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
