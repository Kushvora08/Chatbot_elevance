"""Hosted boundary tests; no cloud accounts or network services are needed."""

import base64
from datetime import datetime, timedelta, timezone
import json
from importlib.util import find_spec
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

HOSTED_AVAILABLE = all(find_spec(name) for name in ("flask", "flask_limiter", "flask_wtf", "waitress"))
if HOSTED_AVAILABLE:
    from kb_pipeline.hosted import create_app
    from kb_pipeline.hosting_runtime import settings_from_environment
    from kb_pipeline.models import HealthReport


@unittest.skipUnless(HOSTED_AVAILABLE, "Install requirements-hosted.txt for hosted tests")
class HostedTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.environment = patch.dict(os.environ, {"ADMIN_USERNAME": "owner", "ADMIN_PASSWORD": "test-only-owner-password", "KB_UPDATE_TIME": "02:00", "KB_MAINTENANCE_START": "02:00", "KB_MAINTENANCE_END": "03:00"})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.settings = {"secret": "test-only-key-" * 4, "base_url": "http://localhost", "secure": False,
                         "data_dir": Path(self.tmp.name), "rate_storage": "memory://", "proxy": False, "seed_demo": True}
        self.app = create_app(self.settings, start_worker=False)
        self.runtime = self.app.extensions["hosted_runtime"]
        self.pipeline = self.runtime.pipeline
        self.client = self.app.test_client()

    def post(self, client, path, data):
        csrf = client.get("/api/auth").json["csrf_token"]
        return client.post(path, json=data, headers={"X-CSRFToken": csrf, "Origin": "http://localhost"})

    def login(self, client=None, username="owner", password="test-only-owner-password"):
        client = client or self.client
        response = self.post(client, "/auth/login", {"username": username, "password": password})
        self.assertEqual(response.status_code, 200, response.json)
        return client

    def customer(self, username):
        user = self.runtime.accounts.create_user(username, "test-only-customer-password")
        client = self.login(self.app.test_client(), username, "test-only-customer-password")
        return client, user

    def start(self, client):
        response = self.post(client, "/api/sessions/start", {})
        self.assertEqual(response.status_code, 200, response.json)
        return {"resume_key": response.json["resume_key"], "session_id": response.json["session"]["id"]}

    def activate(self, hour=2):
        now = datetime(2026, 9, 28, hour, 15, tzinfo=timezone.utc)
        with self.runtime.access.scope("system-test", "admin") as token:
            result = self.pipeline.run_update(token, self.runtime.quality_cases(), now=now)
        return now, result

    def test_anonymous_and_demo_tokens_cannot_access(self):
        self.assertEqual(self.client.get("/").status_code, 302)
        for route in ("/api/admin/status", "/api/sessions/config", "/api/tickets", "/api/status"):
            self.assertEqual(self.client.get(route, headers={"Authorization": "Bearer admin-token"}).status_code, 401)
        payload = self.client.get("/api/auth").json
        self.assertNotIn("token", payload)
        self.assertNotIn("local_token", payload)
        self.assertIsNone(payload["user"])
        self.assertEqual(self.client.post("/auth/logout", json={}).status_code, 401)

    def test_login_requires_csrf_and_matching_origin(self):
        self.assertEqual(self.client.post("/auth/login", json={}).status_code, 400)
        csrf = self.client.get("/api/auth").json["csrf_token"]
        response = self.client.post("/auth/login", json={}, headers={"X-CSRFToken": csrf, "Origin": "https://attacker.example"})
        self.assertEqual(response.status_code, 403)
        self.login()
        self.assertEqual(self.client.get("/api/admin/status").status_code, 200)

    def test_customers_cannot_administer_or_access_legacy_api(self):
        client, _ = self.customer("alice")
        for route in ("/api/admin/status", "/console.html", "/manage.js"):
            self.assertEqual(client.get(route).status_code, 403)
        self.assertEqual(self.post(client, "/api/admin/accounts", {"username": "evil", "password": "a-very-long-password"}).status_code, 403)
        self.assertEqual(self.post(client, "/api/update", {"token": "admin-token"}).status_code, 405)

    def test_customer_session_isolation_and_simulation_rejection(self):
        alice, _ = self.customer("alice")
        bob, _ = self.customer("bob")
        session = self.start(alice)
        self.assertEqual(self.post(bob, "/api/sessions/view", session).status_code, 403)
        self.assertEqual(self.post(alice, "/api/sessions/start", {"now": "2099-01-01"}).status_code, 400)
        self.assertEqual(self.post(alice, "/api/sessions/message", {**session, "message": "Hi", "message_id": "message-1", "role": "admin"}).status_code, 400)

    def test_public_answer_has_citations_and_retries_are_idempotent(self):
        _, update = self.activate()
        self.assertEqual(update.status, "activated", update.to_dict())
        client, _ = self.customer("alice")
        session = self.start(client)
        payload = {**session, "message": "What is the refund policy?", "message_id": "message-1", "product": "Atlas Hub", "region": "US", "language": "en"}
        response = self.post(client, "/api/sessions/message", payload)
        self.assertEqual(response.status_code, 200, response.json)
        self.assertIn("30 days", response.json["reply"])
        self.assertTrue(response.json["citations"])
        source = response.json["citations"][0]
        self.assertEqual(client.get(source["url"]).status_code, 200)
        self.assertTrue(self.post(client, "/api/sessions/message", payload).json["replayed"])

    def test_upload_ownership_paths_and_binary_rejection(self):
        alice, _ = self.customer("alice")
        bob, _ = self.customer("bob")
        upload = self.post(alice, "/api/evidence/upload", {"filename": "invoice.txt", "content_base64": base64.b64encode(b"Order ORD-1234. Amount USD 25.00.").decode()})
        self.assertEqual(upload.status_code, 200, upload.json)
        self.assertNotIn("path", upload.json)
        for refs in ([upload.json["file_id"]], ["../accounts.sqlite3"]):
            self.assertEqual(self.post(bob, "/api/evidence/analyze", {"message": "Check invoice", "file_refs": refs}).status_code, 403)
        response = self.post(alice, "/api/evidence/upload", {"filename": "unsafe.pdf", "content_base64": base64.b64encode(b"%PDF-1.4 malicious").decode()})
        self.assertIn(response.status_code, (400, 403))

    def test_retention_deletes_upload(self):
        client, _ = self.customer("alice")
        upload = self.post(client, "/api/evidence/upload", {"filename": "invoice.txt", "content_base64": base64.b64encode(b"Order ORD-1234. Amount USD 25.00.").decode()}).json
        future = datetime.fromisoformat(upload["delete_after"]) + timedelta(seconds=1)
        with self.runtime.access.scope("system-test", "admin") as token:
            deleted = self.pipeline.cleanup_evidence_uploads(token, future)
        self.assertIn(upload["file_id"], deleted)

    def test_logout_password_reset_disable_and_expiry_revoke_access(self):
        alice, user = self.customer("alice")
        cookie = alice.get_cookie("aster_session").value
        self.assertEqual(self.post(alice, "/auth/logout", {}).status_code, 200)
        alice.set_cookie("aster_session", cookie)
        self.assertEqual(alice.get("/api/sessions/config").status_code, 401)
        self.login(alice, "alice", "test-only-customer-password")
        self.runtime.accounts.reset_password(user["id"], "replacement-test-password")
        self.assertEqual(alice.get("/api/sessions/config").status_code, 401)
        self.login(alice, "alice", "replacement-test-password")
        self.runtime.accounts.disable_user(user["id"], "different-admin")
        self.assertEqual(alice.get("/api/sessions/config").status_code, 401)
        opaque = self.runtime.accounts.issue_session(user["id"], now=0)
        self.assertIsNone(self.runtime.accounts.resolve_session(opaque, now=50000))

    def test_persistence_does_not_overwrite_accounts_documents_or_sessions(self):
        alice, _ = self.customer("alice")
        session = self.start(alice)
        target = Path(self.pipeline.config.storage.source_dir) / "faq.json"
        target.write_text('custom source', encoding="utf-8")
        other_app = create_app(self.settings, start_worker=False)
        self.assertEqual(target.read_text(), "custom source")
        other = other_app.test_client()
        csrf = other.get("/api/auth").json["csrf_token"]
        response = other.post("/auth/login", json={"username": "alice", "password": "test-only-customer-password"}, headers={"X-CSRFToken": csrf})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.post(other, "/api/sessions/view", session).status_code, 200)

    def test_customer_cannot_spoof_ticket_identity(self):
        alice, _ = self.customer("alice")
        bob, _ = self.customer("bob")
        conversation = {"message": "My Atlas Hub is broken", "customer": "Customer", "contact": "test@example.invalid", "product": "Atlas Hub", "customer_id": "spoofed", "conversation_id": "spoofed"}
        first = self.post(alice, "/api/tickets/from-conversation", {**self.start(alice), "conversation": conversation})
        second = self.post(bob, "/api/tickets/from-conversation", {**self.start(bob), "conversation": conversation})
        self.assertEqual(first.status_code, 200, first.json)
        self.assertEqual(second.status_code, 200, second.json)
        self.assertNotEqual(first.json["tickets"][0]["id"], second.json["tickets"][0]["id"])

    def test_maintenance_and_real_time_watch_roll_back(self):
        _, pending = self.activate(hour=12)
        self.assertEqual(pending.status, "approved_pending_maintenance", pending.to_dict())
        self.assertIsNone(self.pipeline.store.active_version_id())
        now = datetime(2026, 9, 29, 2, 15, tzinfo=timezone.utc)
        with self.runtime.access.scope("system-test", "admin") as token:
            result = self.pipeline.activate_pending(token, now=now)
            self.assertEqual(result.status, "activated")
            with patch.object(self.pipeline, "health_check", return_value=HealthReport(status="fail", version_id=result.version_id)):
                self.runtime.check_activation(token, now + timedelta(minutes=4))
        self.assertIsNone(self.pipeline.store.active_version_id())
        self.assertEqual(self.pipeline.store.read_scheduler_state()["next_retry_at"], (now + timedelta(minutes=19)).isoformat())

    def test_health_watch_survives_restart_and_expires(self):
        now, result = self.activate()
        self.assertEqual(result.status, "activated")
        other = create_app(self.settings, start_worker=False).extensions["hosted_runtime"]
        with other.access.scope("system-test", "admin") as token, patch.object(other.pipeline, "health_check", side_effect=RuntimeError("outage")):
            other.check_activation(token, now + timedelta(minutes=5, seconds=1))
        self.assertEqual(other.pipeline.store.active_version_id(), result.version_id)
        self.assertNotIn("activation_health", other.pipeline.store.read_manifest())

    def test_failed_update_retries_and_quality_rejection(self):
        now, initial = self.activate()
        self.assertEqual(initial.status, "activated")
        with self.runtime.access.scope("system-test", "admin") as token:
            with patch.object(self.pipeline, "_build_candidate", side_effect=OSError("temporary outage")):
                for delay in (15, 30, 60):
                    result = self.runtime.scheduler.run_due(token, self.runtime.quality_cases(), now)
                    self.assertEqual(result.status, "failed")
                    now += timedelta(minutes=delay)
                    self.assertEqual(self.pipeline.store.read_scheduler_state()["next_retry_at"], now.isoformat())
            for path in Path(self.pipeline.config.storage.source_dir).glob("*.json"):
                path.write_text("Unrelated content. No refund information.", encoding="utf-8")
            rejected = self.pipeline.run_update(token, self.runtime.quality_cases(), now)
        self.assertEqual(rejected.status, "rejected")
        self.assertEqual(self.pipeline.store.active_version_id(), initial.version_id)

    def test_rate_limit_and_dependency_outage_fail_closed(self):
        for _ in range(5):
            self.assertEqual(self.post(self.client, "/auth/login", {"username": "owner", "password": "wrong"}).status_code, 401)
        self.assertEqual(self.post(self.client, "/auth/login", {"username": "owner", "password": "wrong"}).status_code, 429)
        limiter = next(iter(self.app.extensions["limiter"]))
        with patch.object(limiter.storage, "check", return_value=False):
            self.assertEqual(self.client.get("/healthz").status_code, 503)
        with patch.object(limiter.storage, "incr", side_effect=RuntimeError("private customer@example.invalid")):
            response = self.client.get("/api/auth")
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("customer@example.invalid", response.get_data(as_text=True))

    def test_private_files_headers_and_strong_passwords(self):
        self.login()
        for path in ("/accounts.sqlite3", "/.env", "/app.js", "/api/conversations", "/../pipeline.json"):
            self.assertEqual(self.client.get(path).status_code, 404)
        response = self.client.get("/")
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        self.assertIn("script-src 'self'", response.headers["Content-Security-Policy"])
        response.close()
        self.assertEqual(self.post(self.client, "/api/admin/accounts", {"username": "new-user", "password": "short"}).status_code, 400)
        self.assertNotIn(b"test-only-owner-password", (Path(self.tmp.name) / "accounts.sqlite3").read_bytes())
        self.assertEqual(self.client.get("/healthz", headers={"Host": "attacker.example"}).status_code, 400)

    def test_production_environment_requires_secure_configuration(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ValueError):
                settings_from_environment()
            os.environ.update(APP_SECRET_KEY="strong-test-key-" * 4, APP_BASE_URL="http://example.com", RATELIMIT_STORAGE_URI="redis://localhost")
            with self.assertRaises(ValueError):
                settings_from_environment()
            os.environ["APP_BASE_URL"] = "https://example.com"
            with self.assertRaises(ValueError):
                settings_from_environment()
            os.environ["APP_DATA_DIR"] = self.tmp.name
            self.assertTrue(settings_from_environment()["secure"])

    def test_waitress_configuration_is_valid_for_local_and_hosted(self):
        from waitress.adjustments import Adjustments
        from kb_pipeline.hosted import main
        for proxy in (False, True):
            with patch("kb_pipeline.hosted.settings_from_environment", return_value={**self.settings, "proxy": proxy}), patch("kb_pipeline.hosted.create_app", return_value=self.app), patch("waitress.serve") as serve:
                main()
                Adjustments(**serve.call_args.kwargs)

    def test_new_approval_supersedes_pending_and_new_tests_invalidate_approval(self):
        _, first = self.activate(hour=12)
        self.runtime.finish_update(first)
        path = Path(self.pipeline.config.storage.source_dir) / "faq.json"
        path.write_text(json.dumps({"content": "Atlas Hub includes a USB-C power cable. Its cable is detachable.", "metadata": json.loads(path.read_text())["metadata"]}), encoding="utf-8")
        _, second = self.activate(hour=12)
        self.assertEqual(second.status, "approved_pending_maintenance", second.to_dict())
        self.runtime.finish_update(second)
        self.assertEqual(self.pipeline.store.read_manifest()["pending_versions"], [second.version_id])
        self.assertEqual(self.pipeline.store.read_version(first.version_id).status, "superseded")
        self.runtime.save_cases([{"question": "What is the refund policy?", "required_terms": ["30 days"], "product": "Atlas Hub", "region": "US"}])
        self.assertEqual(self.pipeline.store.read_manifest()["pending_versions"], [])

    def test_secure_cookie_and_missing_snapshot_fail_health(self):
        secure = create_app({**self.settings, "secure": True, "base_url": "https://localhost"}, start_worker=False)
        with secure.test_client().get("/api/auth", base_url="https://localhost") as response:
            cookie = response.headers["Set-Cookie"]
            for flag in ("Secure", "HttpOnly", "SameSite=Lax"):
                self.assertIn(flag, cookie)
        now, result = self.activate()
        with self.runtime.access.scope("system-test", "admin") as token, patch.object(self.pipeline.store, "read_documents", return_value=[]):
            self.runtime.check_activation(token, now + timedelta(minutes=1))
        self.assertIsNone(self.pipeline.store.active_version_id())

    def test_failed_followup_activation_restores_the_previous_version(self):
        now, first = self.activate()
        path = Path(self.pipeline.config.storage.source_dir) / "faq.json"
        document = json.loads(path.read_text())
        document["content"] += " Its cable is detachable."
        path.write_text(json.dumps(document), encoding="utf-8")
        with self.runtime.access.scope("system-test", "admin") as token:
            second = self.pipeline.run_update(token, self.runtime.quality_cases(), now=now + timedelta(minutes=1))
            self.assertEqual(second.status, "activated")
            with patch.object(self.pipeline, "health_check", side_effect=OSError("service unavailable")):
                self.runtime.check_activation(token, now + timedelta(minutes=4))
        self.assertEqual(self.pipeline.store.active_version_id(), first.version_id)
        self.assertEqual(self.pipeline.store.read_version(second.version_id).status, "rolled_back")


if __name__ == "__main__":
    unittest.main()
