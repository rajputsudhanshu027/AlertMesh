"""SQLite persistence for the AlertMesh application state."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime

from .service import Alert, InboxEntry, Person, Place


def _encode(value):
    if isinstance(value, datetime):
        return {"$datetime": value.isoformat()}
    if isinstance(value, set):
        return sorted(value)
    if isinstance(value, dict):
        return {key: _encode(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_encode(item) for item in value]
    return value


def _decode(value):
    if isinstance(value, dict) and set(value) == {"$datetime"}:
        return datetime.fromisoformat(value["$datetime"])
    if isinstance(value, dict):
        return {key: _decode(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_decode(item) for item in value]
    return value


def _tuplify(value):
    if isinstance(value, list):
        return tuple(_tuplify(item) for item in value)
    return value


def _json(value) -> str:
    return json.dumps(_encode(value), separators=(",", ":"))


class SQLiteStore:
    """Transactional SQLite store; the application flow remains provider agnostic."""

    def __init__(self, path: str) -> None:
        self.connection = sqlite3.connect(path)
        self.connection.execute("PRAGMA foreign_keys = ON")
        version = self.connection.execute("PRAGMA user_version").fetchone()[0]
        if version > 1:
            raise RuntimeError(f"Database schema version {version} is newer than this application supports")
        if version < 1:
            self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS people (id TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS places (id TEXT PRIMARY KEY, person_id TEXT NOT NULL REFERENCES people(id) ON DELETE CASCADE, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS alerts (id TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS inbox (
                person_id TEXT NOT NULL REFERENCES people(id) ON DELETE CASCADE,
                alert_id TEXT NOT NULL REFERENCES alerts(id) ON DELETE CASCADE,
                data TEXT NOT NULL, PRIMARY KEY (person_id, alert_id)
            );
            CREATE TABLE IF NOT EXISTS delivery_status (
                person_id TEXT NOT NULL, alert_id TEXT NOT NULL, status TEXT NOT NULL,
                PRIMARY KEY (person_id, alert_id),
                FOREIGN KEY (person_id, alert_id) REFERENCES inbox(person_id, alert_id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS emailed_revision (
                person_id TEXT NOT NULL, alert_id TEXT NOT NULL, data TEXT NOT NULL,
                PRIMARY KEY (person_id, alert_id),
                FOREIGN KEY (person_id, alert_id) REFERENCES inbox(person_id, alert_id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS source_status (source TEXT PRIMARY KEY, last_checked TEXT, stale INTEGER NOT NULL);
            PRAGMA user_version = 1;
            """)
            self.connection.commit()

    @contextmanager
    def transaction(self):
        """Serialize an application update and its reads against other writers."""
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

    def load(self):
        people = {row[0]: Person(**_decode(json.loads(row[1]))) for row in self.connection.execute("SELECT id, data FROM people")}
        places = {row[0]: Place(**_decode(json.loads(row[1]))) for row in self.connection.execute("SELECT id, data FROM places")}
        alerts = {row[0]: Alert(**_decode(json.loads(row[1]))) for row in self.connection.execute("SELECT id, data FROM alerts")}
        inbox = {}
        for person_id, alert_id, raw in self.connection.execute("SELECT person_id, alert_id, data FROM inbox"):
            data = _decode(json.loads(raw))
            data["place_ids"] = set(data["place_ids"])
            inbox[(person_id, alert_id)] = InboxEntry(**data)
        delivery = {(p, a): status for p, a, status in self.connection.execute("SELECT person_id, alert_id, status FROM delivery_status")}
        revisions = {(p, a): _tuplify(_decode(json.loads(data))) for p, a, data in self.connection.execute("SELECT person_id, alert_id, data FROM emailed_revision")}
        source_status = {s: (datetime.fromisoformat(checked) if checked else None, bool(stale)) for s, checked, stale in self.connection.execute("SELECT source, last_checked, stale FROM source_status")}
        return people, places, alerts, inbox, delivery, revisions, source_status

    def save(self, people, places, alerts, inbox, delivery, revisions, last_checked, source_stale) -> None:
        owns_transaction = not self.connection.in_transaction
        if owns_transaction:
            self.connection.execute("BEGIN IMMEDIATE")
        try:
            self._save_rows(people, places, alerts, inbox, delivery, revisions, last_checked, source_stale)
            if owns_transaction:
                self.connection.commit()
        except Exception:
            if owns_transaction:
                self.connection.rollback()
            raise

    def _save_rows(self, people, places, alerts, inbox, delivery, revisions, last_checked, source_stale) -> None:
        for table in ("delivery_status", "emailed_revision", "inbox", "places", "people", "alerts", "source_status"):
            self.connection.execute(f"DELETE FROM {table}")
        self.connection.executemany("INSERT INTO people VALUES (?, ?)", ((key, _json(asdict(value))) for key, value in people.items()))
        self.connection.executemany("INSERT INTO places VALUES (?, ?, ?)", ((key, value.person_id, _json(asdict(value))) for key, value in places.items()))
        self.connection.executemany("INSERT INTO alerts VALUES (?, ?)", ((key, _json(asdict(value))) for key, value in alerts.items()))
        self.connection.executemany("INSERT INTO inbox VALUES (?, ?, ?)", ((p, a, _json(asdict(value))) for (p, a), value in inbox.items()))
        self.connection.executemany("INSERT INTO delivery_status VALUES (?, ?, ?)", ((p, a, status) for (p, a), status in delivery.items()))
        self.connection.executemany("INSERT INTO emailed_revision VALUES (?, ?, ?)", ((p, a, _json(revision)) for (p, a), revision in revisions.items()))
        sources = set(last_checked) | set(source_stale)
        self.connection.executemany("INSERT INTO source_status VALUES (?, ?, ?)", ((source, last_checked.get(source).isoformat() if last_checked.get(source) else None, int(source_stale.get(source, True))) for source in sources))

    def close(self) -> None:
        self.connection.close()
