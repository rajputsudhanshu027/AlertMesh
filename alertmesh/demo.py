"""Deterministic, offline replay of representative AlertMesh behavior.

Run with ``python -m alertmesh.demo``. The command creates a fresh in-memory
SQLite database, seeds People and Places, then replays the same normalized source
alerts twice to show that inbox entries and initial email effects are stable.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from .service import Alert, AlertMesh, Person, Place


NOW = datetime.now(timezone.utc).replace(microsecond=0)
WEATHER_AREA = {
    "type": "Polygon",
    "coordinates": [[[-78.0, 37.0], [-76.0, 37.0], [-76.0, 39.0], [-78.0, 39.0], [-78.0, 37.0]]],
}


class DemoEmail:
    """In-memory mail adapter; never contacts an email provider."""

    def __init__(self) -> None:
        self.messages: list[tuple[str, str, str]] = []
        self.fail_next = False

    def send(self, to: str, subject: str, body: str) -> None:
        if self.fail_next:
            self.fail_next = False
            raise OSError("simulated temporary mail outage")
        self.messages.append((to, subject, body))


def seeded_alerts() -> list[Alert]:
    """Return stable normalized NWS and USGS source alerts."""
    return [
        Alert("nws:demo-storm", "nws", "weather", "Demo severe storm", NOW,
              severity="severe", instructions="Move to shelter", geometry=WEATHER_AREA),
        Alert("usgs:demo-quake", "usgs", "earthquake", "Demo nearby earthquake", NOW,
              latitude=38.0, longitude=-77.0),
        Alert("usgs:demo-expired", "usgs", "earthquake", "Demo ended earthquake", NOW,
              latitude=38.0, longitude=-77.0),
    ]


def run_demo() -> dict[str, int]:
    """Seed and replay the demo in a fresh, isolated SQLite database."""
    mail = DemoEmail()
    app = AlertMesh(mail, db_path=":memory:")
    try:
        app.add_person(Person("demo-person", "demo@example.test", verified=True))
        app.add_place(Place("demo-home", "demo-person", "Home", 38.0, -77.0, radius_km=30))
        app.add_place(Place("demo-work", "demo-person", "Work", 38.01, -77.01, radius_km=30))

        alerts = seeded_alerts()
        for alert in alerts[:2]:
            app.ingest(alert, checked_at=NOW)
        app.ingest(alerts[2], checked_at=NOW)
        # Replaying the same source snapshot is idempotent for inbox and email.
        original_email_count = len(mail.messages)
        for alert in alerts[:2]:
            app.ingest(alert, checked_at=NOW)
        duplicate_email_count = len(mail.messages)

        # An active alert discovered after a Place is added is visible without
        # sending a backdated message.
        app.add_place(Place("demo-cabin", "demo-person", "Cabin", 38.02, -77.02, radius_km=30),
                      [alerts[1]])
        expired_revision = replace(alerts[2], status="expired", ended_at=NOW + timedelta(minutes=1))
        app.ingest(expired_revision, checked_at=NOW + timedelta(minutes=1))

        # A wording-only revision is non-material; severity change is material.
        app.ingest(replace(alerts[0], title="Storm wording revised"), checked_at=NOW + timedelta(minutes=1))
        app.ingest(replace(alerts[0], severity="extreme"), checked_at=NOW + timedelta(minutes=2))

        # Exercise an email retry on a separate alert, then source freshness.
        retry_alert = Alert("usgs:demo-retry", "usgs", "earthquake", "Retry demo earthquake", NOW,
                            latitude=38.0, longitude=-77.0)
        mail.fail_next = True
        app.ingest(retry_alert, checked_at=NOW + timedelta(minutes=3))
        app.retry_failed_deliveries()
        app.mark_source_stale("nws")
        stale = app.source_status("nws")["stale"]

        app.ingest(expired_revision, checked_at=NOW + timedelta(minutes=1))
        replay_inbox_before_delete = app.inbox_for("demo-person")
        terminal_retained = any(e.alert_id == "usgs:demo-expired" for e in replay_inbox_before_delete)
        retry_sent = app.delivery_status.get(("demo-person", "usgs:demo-retry")) == "sent"
        quake_entry = next(e for e in replay_inbox_before_delete if e.alert_id == "usgs:demo-quake")

        # Demonstrate deletion effects through public inbox reads.
        app.delete_place("demo-cabin")
        after_place_delete = app.inbox_for("demo-person")
        app.delete_person("demo-person")
        after_account_delete = app.inbox_for("demo-person")
        return {
            "inbox_entries_after_replay": len(replay_inbox_before_delete),
            "email_messages_after_replay": duplicate_email_count,
            "initial_email_count": original_email_count,
            "quake_matching_places": len(quake_entry.place_ids),
            "places_after_place_delete": len(quake_entry.place_ids) - int("demo-cabin" in quake_entry.place_ids),
            "place_delete_remaining_entries": len(after_place_delete),
            "account_delete_remaining_entries": len(after_account_delete),
            "source_was_stale": int(bool(stale)),
            "expired_alert_retained": int(terminal_retained),
            "retry_delivery_sent": int(retry_sent),
        }
    finally:
        app.close()


def main() -> None:
    result = run_demo()
    for name, value in result.items():
        print(f"{name}: {value}")


if __name__ == "__main__":
    main()
