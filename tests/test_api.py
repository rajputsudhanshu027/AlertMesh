import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from alertmesh import Alert, AlertMesh
from alertmesh.api import AlertMeshAPI, VerificationMailer


class FakeMailer(VerificationMailer):
    def __init__(self):
        self.sent = []
        self.alerts = []

    def send_verification(self, email, token):
        self.sent.append((email, token))

    def send(self, to, subject, body):
        self.alerts.append((to, subject, body))


class APITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.mailer = FakeMailer()
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.app = AlertMeshAPI(f"{self.temp.name}/test.sqlite3", self.mailer, clock=lambda: self.now)

    def tearDown(self):
        self.app.close()
        self.temp.cleanup()

    def request(self, method, path, body=None, token=None):
        raw = json.dumps(body or {}).encode()
        environ = {
            "REQUEST_METHOD": method,
            "PATH_INFO": path,
            "CONTENT_LENGTH": str(len(raw)),
            "CONTENT_TYPE": "application/json",
            "wsgi.input": io.BytesIO(raw),
        }
        if token:
            environ["HTTP_AUTHORIZATION"] = f"Bearer {token}"
        captured = {}

        def start_response(status, headers):
            captured["status"] = int(status.split()[0])
            captured["headers"] = headers

        response = b"".join(self.app(environ, start_response))
        return captured["status"], json.loads(response)

    def register_and_login(self, email="person@example.com"):
        status, result = self.request("POST", "/api/register", {"email": email, "password": "a-strong-password"})
        self.assertEqual(status, 201)
        self.assertEqual(self.mailer.sent[-1][0], email)
        status, result = self.request("POST", "/api/verify", {"token": self.mailer.sent[-1][1]})
        self.assertEqual(status, 200)
        status, result = self.request("POST", "/api/login", {"email": email, "password": "a-strong-password"})
        self.assertEqual(status, 200)
        return result["access_token"]

    def test_verification_authentication_and_persistent_place_crud(self):
        status, _ = self.request("GET", "/api/me")
        self.assertEqual(status, 401)
        token = self.register_and_login()
        status, profile = self.request("GET", "/api/me", token=token)
        self.assertEqual((status, profile["verified"]), (200, True))
        status, place = self.request("POST", "/api/places", {"name": "Home", "latitude": 38, "longitude": -77}, token)
        self.assertEqual(status, 201)
        self.assertEqual(place["minimum_severity"], "moderate")
        place_id = place["id"]
        status, places = self.request("GET", "/api/places", token=token)
        self.assertEqual((status, places["places"][0]["name"]), (200, "Home"))
        status, updated = self.request("PATCH", f"/api/places/{place_id}", {"name": "Office", "earthquake_enabled": False}, token)
        self.assertEqual((status, updated["name"], updated["earthquake_enabled"]), (200, "Office", False))
        database = self.app.database
        self.app.close()
        self.app = AlertMeshAPI(database, self.mailer, clock=lambda: self.now)
        status, places = self.request("GET", "/api/places", token=token)
        self.assertEqual(places["places"][0]["name"], "Office")
        self.assertEqual(self.request("DELETE", f"/api/places/{place_id}", token=token)[0], 200)

    def test_unverified_login_and_other_person_place_access_are_rejected(self):
        self.request("POST", "/api/register", {"email": "one@example.com", "password": "a-strong-password"})
        status, _ = self.request("POST", "/api/login", {"email": "one@example.com", "password": "a-strong-password"})
        self.assertEqual(status, 403)
        first_token = self.register_and_login("two@example.com")
        second_token = self.register_and_login("three@example.com")
        _, place = self.request("POST", "/api/places", {"name": "Private", "latitude": 38, "longitude": -77}, first_token)
        status, _ = self.request("PATCH", f"/api/places/{place['id']}", {"name": "Stolen"}, second_token)
        self.assertEqual(status, 404)

    def test_registration_rejects_malformed_email(self):
        status, error = self.request("POST", "/api/register", {"email": "a@@b", "password": "a-strong-password"})
        self.assertEqual(status, 400)
        self.assertIn("valid email", error["error"])
        self.assertEqual(self.mailer.sent, [])

    def test_account_delete_restores_credentials_when_shared_flow_delete_fails(self):
        token = self.register_and_login()
        with patch.object(self.app.flow, "delete_person", side_effect=RuntimeError("temporary storage failure")):
            status, error = self.request("DELETE", "/api/me", token=token)
        self.assertEqual(status, 503)
        self.assertIn("Could not delete account", error["error"])
        self.assertEqual(self.request("GET", "/api/me", token=token)[0], 200)

    def test_invalid_place_and_account_deletion(self):
        token = self.register_and_login()
        status, error = self.request("POST", "/api/places", {"name": "Bad", "latitude": 100, "longitude": 0}, token)
        self.assertEqual(status, 400)
        self.assertIn("Coordinates", error["error"])
        self.assertEqual(self.request("DELETE", "/api/me", token=token)[0], 200)
        self.assertEqual(self.request("GET", "/api/me", token=token)[0], 401)

    def test_api_place_matches_ingested_alert_and_inbox_reads_shared_flow(self):
        token = self.register_and_login()
        _, profile = self.request("GET", "/api/me", token=token)
        person_id = profile["id"]
        status, place = self.request("POST", "/api/places", {"name": "Home", "latitude": 38, "longitude": -77}, token)
        self.assertEqual(status, 201)

        source = AlertMesh(self.mailer, db_path=self.app.database)
        source.ingest(Alert("usgs:api-flow", "usgs", "earthquake", "Nearby quake", self.now, latitude=38, longitude=-77))
        source.close()

        status, inbox = self.request("GET", "/api/inbox", token=token)
        self.assertEqual(status, 200)
        self.assertEqual(len(inbox["alerts"]), 1)
        self.assertEqual(inbox["alerts"][0]["places"], [{"id": place["id"], "name": "Home"}])
        self.assertEqual(inbox["alerts"][0]["delivery"]["status"], "sent")
        self.assertEqual(inbox["sources"][0]["source"], "usgs")

        self.assertEqual(self.request("DELETE", "/api/me", token=token)[0], 200)
        remaining = AlertMesh(self.mailer, db_path=self.app.database)
        self.assertNotIn(person_id, remaining.people)
        self.assertNotIn(place["id"], remaining.places)
        self.assertEqual(remaining.inbox_for(person_id), [])
        remaining.close()

    def test_verification_token_expires(self):
        self.request("POST", "/api/register", {"email": "late@example.com", "password": "a-strong-password"})
        self.now += timedelta(hours=25)
        status, result = self.request("POST", "/api/verify", {"token": self.mailer.sent[-1][1]})
        self.assertEqual(status, 400)
        self.assertIn("expired", result["error"])


if __name__ == "__main__":
    unittest.main()
