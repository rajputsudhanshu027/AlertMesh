from datetime import datetime, timezone
import unittest

from alertmesh import Alert, AlertMesh, Person, Place


class FakeEmail:
    def __init__(self):
        self.messages = []
        self.fail = False

    def send(self, to, subject, body):
        if self.fail:
            self.fail = False
            raise OSError("temporary mail outage")
        self.messages.append((to, subject, body))


class AlertMeshFlowTests(unittest.TestCase):
    def setUp(self):
        self.mail = FakeEmail()
        self.app = AlertMesh(self.mail)
        self.app.add_person(Person("p1", "person@example.com", verified=True))
        self.app.add_place(Place("home", "p1", "Home", 38.0, -77.0, radius_km=30))

    def test_earthquake_matches_once_across_places_and_deduplicates_ingestion(self):
        self.app.add_place(Place("work", "p1", "Work", 38.01, -77.01))
        alert = Alert("usgs:1", "usgs", "earthquake", "Nearby quake", datetime.now(timezone.utc), latitude=38, longitude=-77)
        self.app.ingest(alert)
        self.app.ingest(alert)
        entries = self.app.inbox_for("p1")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].place_ids, {"home", "work"})
        self.assertEqual(len(self.mail.messages), 1)

    def test_weather_uses_source_area_and_unknown_severity_does_not_email(self):
        area = {"type": "Polygon", "coordinates": [[[-78, 37], [-76, 37], [-76, 39], [-78, 39], [-78, 37]]]}
        alert = Alert("nws:1", "nws", "weather", "Storm", datetime.now(timezone.utc), severity="unknown", geometry=area)
        self.app.ingest(alert)
        self.assertEqual(self.app.inbox_for("p1")[0].place_ids, {"home"})
        self.assertEqual(self.mail.messages, [])

    def test_weather_below_place_minimum_severity_is_not_matched(self):
        area = {"type": "Polygon", "coordinates": [[[-78, 37], [-76, 37], [-76, 39], [-78, 39], [-78, 37]]]}
        alert = Alert("nws:minor", "nws", "weather", "Minor alert", datetime.now(timezone.utc), severity="minor", geometry=area)
        self.app.ingest(alert)
        self.assertEqual(self.app.inbox_for("p1"), [])

    def test_canceled_alert_remains_in_history_for_ninety_days(self):
        point = Alert("usgs:done", "usgs", "earthquake", "Quake", datetime.now(timezone.utc), latitude=38, longitude=-77)
        self.app.ingest(point)
        point.status = "cancelled"
        self.app.ingest(point)
        self.assertEqual(len(self.app.inbox_for("p1")), 1)

    def test_active_alert_catchup_has_no_backdated_email(self):
        alert = Alert("usgs:old", "usgs", "earthquake", "Old quake", datetime.now(timezone.utc), latitude=38, longitude=-77)
        self.app.add_place(Place("cabin", "p1", "Cabin", 38, -77), [alert])
        self.assertEqual(len(self.app.inbox_for("p1")), 1)
        self.assertEqual(self.mail.messages, [])

    def test_material_revision_emails_but_title_only_revision_does_not(self):
        alert = Alert("nws:2", "nws", "weather", "Storm", datetime.now(timezone.utc), severity="severe", instructions="Stay inside", geometry={"type": "Polygon", "coordinates": [[[-78, 37], [-76, 37], [-76, 39], [-78, 39], [-78, 37]]]})
        self.app.ingest(alert)
        self.app.ingest(Alert(**{**alert.__dict__, "title": "Storm warning updated wording"}))
        self.assertEqual(len(self.mail.messages), 1)
        self.app.ingest(Alert(**{**alert.__dict__, "severity": "extreme"}))
        self.assertEqual(len(self.mail.messages), 2)

    def test_failures_can_be_retried_and_deletion_removes_personal_data(self):
        alert = Alert("usgs:3", "usgs", "earthquake", "Quake", datetime.now(timezone.utc), latitude=38, longitude=-77)
        self.mail.fail = True
        self.app.ingest(alert)
        self.assertEqual(self.app.delivery_status[("p1", "usgs:3")], "failed")
        self.app.retry_failed_deliveries()
        self.assertEqual(self.app.delivery_status[("p1", "usgs:3")], "sent")
        self.app.delete_person("p1")
        self.assertEqual(self.app.inbox_for("p1"), [])
        self.assertNotIn("home", self.app.places)


if __name__ == "__main__":
    unittest.main()
