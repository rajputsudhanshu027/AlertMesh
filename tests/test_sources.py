import tempfile
import unittest
from datetime import datetime, timezone

from alertmesh import AlertMesh, Person, Place
from alertmesh.sources import (
    IngestionRunner,
    NWSAdapter,
    SourceIdentityStore,
    TransientSourceError,
    USGSAdapter,
)


class FakeTransport:
    def __init__(self, responses):
        self.responses = responses
        self.requests = []

    def get_json(self, url, *, headers=None):
        self.requests.append(url)
        if isinstance(self.responses, list):
            return self.responses.pop(0)
        response = self.responses[url]
        return response.pop(0) if isinstance(response, list) else response


class FakeEmail:
    def __init__(self):
        self.messages = []

    def send(self, to, subject, body):
        self.messages.append((to, subject, body))


class SequenceSource:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    def fetch(self):
        self.calls += 1
        result = self.outcomes.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def nws_feature(source_id, *, reference=None, message_type="Alert", severity="Severe", ends="2026-05-01T00:00:00+00:00"):
    references = [{"id": reference, "sender": "w-nws.webmaster@noaa.gov", "sent": "2026-04-01T00:00:00+00:00"}] if reference else []
    return {
        "type": "Feature",
        "id": f"https://api.weather.gov/alerts/{source_id}",
        "geometry": {"type": "Polygon", "coordinates": [[[-78, 37], [-76, 37], [-76, 39], [-78, 39], [-78, 37]]]},
        "properties": {
            "id": source_id,
            "sent": "2026-04-01T00:00:00+00:00",
            "effective": "2026-04-01T00:00:00+00:00",
            "ends": ends,
            "status": "Actual",
            "messageType": message_type,
            "severity": severity,
            "event": "Wind Warning",
            "headline": "High winds expected",
            "description": "Strong winds are expected.",
            "instruction": "Secure loose objects.",
            "references": references,
        },
    }


class SourceAdapterTests(unittest.TestCase):
    def test_nws_normalizes_geometry_and_revisions_to_one_alert_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            identity = SourceIdentityStore(f"{directory}/alerts.sqlite3")
            original = {"type": "FeatureCollection", "features": [nws_feature("urn:nws:original")]}
            revision = {"type": "FeatureCollection", "features": [nws_feature("urn:nws:update", reference="urn:nws:original", severity="Extreme")]}
            transport = FakeTransport({
                NWSAdapter.ACTIVE_FEED_URL: [original, revision],
                NWSAdapter.RECENT_FEED_URL: [{"features": []}, {"features": []}],
            })
            adapter = NWSAdapter(transport, identity, clock=lambda: datetime(2026, 4, 1, tzinfo=timezone.utc))
            original = adapter.fetch()[0]
            revision = adapter.fetch()[0]
            self.assertEqual(original.id, "nws:urn:nws:original")
            self.assertEqual(revision.id, original.id)
            self.assertEqual(revision.severity, "extreme")
            self.assertEqual(revision.geometry["type"], "Polygon")
            self.assertEqual(revision.instructions, "Secure loose objects.")
            identity.close()
            identity = SourceIdentityStore(f"{directory}/alerts.sqlite3")
            restarted = NWSAdapter(FakeTransport({
                NWSAdapter.ACTIVE_FEED_URL: {"features": [nws_feature("urn:nws:followup", reference="urn:nws:update")]},
                NWSAdapter.RECENT_FEED_URL: {"features": []},
            }), identity, clock=lambda: datetime(2026, 4, 1, tzinfo=timezone.utc))
            self.assertEqual(restarted.fetch()[0].id, original.id)
            identity.close()

    def test_nws_cancel_and_expiry_are_normalized(self):
        with tempfile.TemporaryDirectory() as directory:
            identity = SourceIdentityStore(f"{directory}/alerts.sqlite3")
            cancel = nws_feature("urn:nws:cancel", message_type="Cancel")
            expired = nws_feature("urn:nws:expired", ends="2026-03-31T23:00:00+00:00")
            feed = {"features": [cancel, expired]}
            adapter = NWSAdapter(FakeTransport({NWSAdapter.ACTIVE_FEED_URL: feed, NWSAdapter.RECENT_FEED_URL: feed}), identity, clock=lambda: datetime(2026, 4, 1, tzinfo=timezone.utc))
            alerts = adapter.fetch()
            self.assertEqual([alert.status for alert in alerts], ["cancelled", "expired"])
            self.assertIsNotNone(alerts[0].ended_at)
            identity.close()

    def test_usgs_normalizes_stable_id_origin_time_and_coordinates(self):
        feed = {"type": "FeatureCollection", "features": [{
            "id": "us7000abcd",
            "properties": {"time": 1775000000000, "updated": 1775000300000, "title": "M 2.1 - 4 km NE of Example"},
            "geometry": {"type": "Point", "coordinates": [-77.2, 38.1, 5.0]},
        }]}
        adapter = USGSAdapter(FakeTransport({USGSAdapter.FEED_URL: feed}))
        alert = adapter.fetch()[0]
        self.assertEqual(alert.id, "usgs:us7000abcd")
        self.assertEqual((alert.latitude, alert.longitude), (38.1, -77.2))
        self.assertEqual(alert.geometry, {"type": "Point", "coordinates": [-77.2, 38.1]})
        self.assertEqual(alert.published_at, datetime.fromtimestamp(1775000000, timezone.utc))
        self.assertEqual(alert.status, "active")

    def test_runner_retries_transient_source_errors_and_flows_alert_to_inbox(self):
        with tempfile.TemporaryDirectory() as directory:
            email = FakeEmail()
            app = AlertMesh(email, db_path=f"{directory}/alerts.sqlite3")
            app.add_person(Person("person", "person@example.com", verified=True))
            app.add_place(Place("home", "person", "Home", 38, -77))
            alert = USGSAdapter(FakeTransport({USGSAdapter.FEED_URL: {"features": []}})).normalize_feature({
                "id": "us7000flow", "properties": {"time": 1775000000000, "title": "Nearby quake"},
                "geometry": {"type": "Point", "coordinates": [-77, 38, 4]},
            })
            source = SequenceSource([TransientSourceError("temporary outage"), TransientSourceError("temporary outage"), [alert]])
            delays = []
            runner = IngestionRunner(app, {"usgs": source}, max_attempts=3, base_delay=0.25, max_delay=0.5, sleep=delays.append, clock=lambda: datetime(2026, 4, 1, tzinfo=timezone.utc))
            result = runner.run_source("usgs")
            self.assertTrue(result.success)
            self.assertEqual(result.attempts, 3)
            self.assertEqual(delays, [0.25, 0.5])
            self.assertEqual(len(app.inbox_for("person")), 1)
            self.assertEqual(len(email.messages), 1)
            self.assertFalse(app.source_status("usgs")["stale"])
            app.close()

    def test_runner_marks_failed_source_stale_and_preserves_last_known_alerts(self):
        with tempfile.TemporaryDirectory() as directory:
            app = AlertMesh(FakeEmail(), db_path=f"{directory}/alerts.sqlite3")
            app.add_person(Person("person", "person@example.com"))
            app.add_place(Place("home", "person", "Home", 38, -77))
            alert = USGSAdapter(FakeTransport({})).normalize_feature({
                "id": "us7000known", "properties": {"time": 1775000000000, "title": "Known quake"},
                "geometry": {"type": "Point", "coordinates": [-77, 38, 4]},
            })
            app.ingest(alert)
            source = SequenceSource([TransientSourceError("offline"), TransientSourceError("offline")])
            runner = IngestionRunner(app, {"usgs": source}, max_attempts=2, sleep=lambda _: None)
            result = runner.run_source("usgs")
            self.assertFalse(result.success)
            self.assertTrue(app.source_status("usgs")["stale"])
            self.assertEqual(len(app.inbox_for("person")), 1)
            app.close()

    def test_empty_successful_fetch_still_records_freshness(self):
        with tempfile.TemporaryDirectory() as directory:
            checked_at = datetime(2026, 4, 1, tzinfo=timezone.utc)
            app = AlertMesh(FakeEmail(), db_path=f"{directory}/alerts.sqlite3")
            runner = IngestionRunner(app, {"nws": SequenceSource([[]])}, clock=lambda: checked_at)
            result = runner.run_source("nws")
            status = app.source_status("nws")
            self.assertTrue(result.success)
            self.assertEqual(result.received, 0)
            self.assertEqual(status, {"source": "nws", "stale": False, "last_checked": checked_at})
            app.close()


if __name__ == "__main__":
    unittest.main()
