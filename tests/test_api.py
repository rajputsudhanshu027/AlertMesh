import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from alertmesh import Alert, AlertMesh
from alertmesh.api import AlertMeshAPI, VerificationMailer
from alertmesh.geocoding import GeocodedPlace


class FakeMailer(VerificationMailer):
    def __init__(self):
        self.sent = []
        self.alerts = []
        self.fail_verification = False

    def send_verification(self, email, token):
        if self.fail_verification:
            raise OSError("mail service unavailable")
        self.sent.append((email, token))

    def send(self, to, subject, body):
        self.alerts.append((to, subject, body))


class FakeGeocoder:
    def __init__(self):
        self.fail = False
        self.results = [GeocodedPlace("Washington, Virginia, United States", 38, -77, "US-VA")]

    def search(self, query):
        if self.fail:
            raise OSError("provider unavailable")
        return self.results if "washington" in query.lower() else []

    def reverse(self, latitude, longitude):
        if (latitude, longitude) == (64.0, -150.0):
            return GeocodedPlace("Alaska, United States", latitude, longitude, "US-AK")
        if (latitude, longitude) == (21.0, -157.0):
            return GeocodedPlace("Hawaii, United States", latitude, longitude, "US-HI")
        if (latitude, longitude) == (38.0, -77.0):
            return self.results[0]
        return None


class APITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.mailer = FakeMailer()
        self.geocoder = FakeGeocoder()
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.app = AlertMeshAPI(f"{self.temp.name}/test.sqlite3", self.mailer, clock=lambda: self.now, geocoder=self.geocoder)

    def tearDown(self):
        self.app.close()
        self.temp.cleanup()

    def request(self, method, path, body=None, token=None, query="", cookie=None, browser=False):
        raw = json.dumps(body or {}).encode()
        environ = {
            "REQUEST_METHOD": method,
            "PATH_INFO": path,
            "QUERY_STRING": query,
            "CONTENT_LENGTH": str(len(raw)),
            "CONTENT_TYPE": "application/json",
            "wsgi.input": io.BytesIO(raw),
        }
        if token:
            environ["HTTP_AUTHORIZATION"] = f"Bearer {token}"
        if cookie:
            environ["HTTP_COOKIE"] = cookie
        if browser:
            environ["HTTP_X_ALERTMESH_BROWSER"] = "session-cookie"
        captured = {}

        def start_response(status, headers):
            captured["status"] = int(status.split()[0])
            captured["headers"] = headers

        response = b"".join(self.app(environ, start_response))
        self.last_headers = dict(captured["headers"])
        return captured["status"], json.loads(response)

    def test_browser_session_cookie_authenticates_and_logout_clears_it(self):
        self.request("POST", "/api/register", {"email": "browser@example.com", "password": "a-strong-password"})
        token = self.mailer.sent[-1][1]
        self.request("POST", "/api/verify", {"token": token})
        status, result = self.request("POST", "/api/login", {"email": "browser@example.com", "password": "a-strong-password"}, browser=True)
        self.assertEqual(status, 200)
        cookie = self.last_headers["Set-Cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Lax", cookie)
        self.assertNotIn("access_token", result)
        session_cookie = cookie.split(";", 1)[0]
        self.assertEqual(self.request("GET", "/api/me", cookie=session_cookie)[0], 200)
        self.assertEqual(self.request("POST", "/api/logout", cookie=session_cookie)[0], 200)
        self.assertIn("Max-Age=0", self.last_headers["Set-Cookie"])
        self.assertEqual(self.request("GET", "/api/me", cookie=session_cookie)[0], 401)

    def test_browser_sessions_are_isolated_between_people(self):
        first = self.register_and_login("first-browser@example.com")
        first_cookie = f"alertmesh_session={first}"
        second = self.register_and_login("second-browser@example.com")
        second_cookie = f"alertmesh_session={second}"
        self.assertEqual(self.request("GET", "/api/me", cookie=first_cookie)[1]["email"], "first-browser@example.com")
        self.assertEqual(self.request("GET", "/api/me", cookie=second_cookie)[1]["email"], "second-browser@example.com")
        self.request("POST", "/api/logout", cookie=first_cookie)
        self.assertEqual(self.request("GET", "/api/me", cookie=first_cookie)[0], 401)
        self.assertEqual(self.request("GET", "/api/me", cookie=second_cookie)[0], 200)

    def test_browser_page_offers_account_access_and_retains_public_demo(self):
        captured = {}
        environ = {"REQUEST_METHOD": "GET", "PATH_INFO": "/", "CONTENT_LENGTH": "0", "wsgi.input": io.BytesIO(b"")}

        def start_response(status, headers):
            captured["status"] = int(status.split()[0])

        page = b"".join(self.app(environ, start_response)).decode()
        self.assertEqual(captured["status"], 200)
        self.assertIn('id="account-access"', page)
        self.assertIn('data-auth-mode="register"', page)
        self.assertIn('data-auth-mode="verify"', page)
        self.assertIn('data-auth-mode="resend"', page)
        self.assertIn('id="return-demo"', page)
        self.assertIn('loadDemo();', page)

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
        self.app = AlertMeshAPI(database, self.mailer, clock=lambda: self.now, geocoder=self.geocoder)
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

    def test_geocoder_lookup_handles_results_empty_and_provider_errors(self):
        token = self.register_and_login()
        status, result = self.request("GET", "/api/geocode", token=token, query="q=Washington")
        self.assertEqual(status, 200)
        self.assertEqual(result["results"][0]["state_code"], "US-VA")
        self.assertIn("OpenStreetMap", result["attribution"])
        self.geocoder.results.append(GeocodedPlace("Honolulu, Hawaii, United States", 21, -157, "US-HI"))
        status, result = self.request("GET", "/api/geocode", token=token, query="q=Washington")
        self.assertEqual((status, len(result["results"])), (200, 1))
        self.assertEqual(self.request("GET", "/api/geocode", token=token, query="q=Atlantis")[1]["results"], [])
        self.geocoder.fail = True
        status, error = self.request("GET", "/api/geocode", token=token, query="q=Washington")
        self.assertEqual(status, 503)
        self.assertIn("unavailable", error["error"])

    def test_place_must_be_contiguous_us_and_use_radius_preset(self):
        token = self.register_and_login()
        status, error = self.request("POST", "/api/places", {"name": "Alaska", "latitude": 64, "longitude": -150}, token)
        self.assertEqual(status, 400)
        self.assertIn("contiguous", error["error"])
        status, error = self.request("POST", "/api/places", {"name": "Hawaii", "latitude": 21, "longitude": -157}, token)
        self.assertEqual(status, 400)
        status, error = self.request("POST", "/api/places", {"name": "Home", "latitude": 38, "longitude": -77, "radius_km": 20}, token)
        self.assertEqual(status, 400)
        self.assertIn("one of", error["error"])

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
        status, metrics = self.request("GET", "/api/metrics", token=token)
        self.assertEqual(status, 200)
        self.assertGreaterEqual(metrics["uptime_seconds"], 0)
        self.assertGreaterEqual(metrics["api_p95_latency_ms"], 0)
        self.assertEqual(metrics["delivery"]["status_counts"]["sent"], 1)
        self.assertEqual(metrics["sources"][0]["stale"], False)
        serialized_metrics = json.dumps(metrics)
        self.assertNotIn(person_id, serialized_metrics)
        self.assertNotIn("person@example.com", serialized_metrics)
        self.assertNotIn("Home", serialized_metrics)

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
        status, result = self.request("POST", "/api/resend-verification", {"email": "late@example.com", "password": "a-strong-password"})
        self.assertEqual(status, 200)
        self.assertIn("fresh verification token", result["message"])
        status, result = self.request("POST", "/api/verify", {"token": self.mailer.sent[-1][1]})
        self.assertEqual((status, result["verified"]), (200, True))

    def test_verification_delivery_failure_has_recovery_message(self):
        self.mailer.fail_verification = True
        status, result = self.request("POST", "/api/register", {"email": "offline@example.com", "password": "a-strong-password"})
        self.assertEqual(status, 503)
        self.assertIn("Could not send verification email", result["error"])
        self.assertIn("try again", result["error"])


if __name__ == "__main__":
    unittest.main()
