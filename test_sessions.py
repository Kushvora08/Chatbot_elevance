from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from time import monotonic, sleep
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import json
import unittest

from kb_pipeline.config import PipelineConfig, SessionConfig
from kb_pipeline.errors import AccessDenied
from kb_pipeline.languages import LanguageInterpreter
from kb_pipeline.models import QueryResult
from kb_pipeline.orchestrator import KnowledgeBasePipeline
from kb_pipeline.server import create_server
from kb_pipeline.sessions import empty_memory
from kb_pipeline.tickets import timestamp


NOW = timestamp("2026-09-17T09:00:00Z")


class LanguageTest(unittest.TestCase):
    def setUp(self):
        self.parser = LanguageInterpreter(SessionConfig())

    def parse(self, text, **changes):
        return self.parser.interpret(text, changes.pop("memory", empty_memory("en")), **changes)

    def test_native_languages_and_unseen_wording(self):
        for code, message, intent in [
            ("ta", "எனக்கு பணம் திரும்ப வேண்டும்", "refund"),
            ("ta", "என் ஆர்டர் ORD-14 எப்போது கிடைக்கும்?", "order_status"),
            ("te", "నాకు డబ్బు తిరిగి కావాలి", "refund"),
            ("te", "నా ఆర్డర్ ORD-14 ఎక్కడ ఉంది?", "order_status"),
            ("bn", "আমার টাকা ফেরত চাই", "refund"),
            ("bn", "আমার অর্ডার ORD-14 কখন আসবে?", "order_status"),
        ]:
            with self.subTest(code=code, message=message):
                result = self.parse(message)
                self.assertEqual(result["language"], code)
                self.assertFalse(result["needs_clarification"])
                self.assertIn(intent, [r["intent"] for r in result["requests"]])

    def test_transliterated_and_mixed_messages(self):
        for code, message in [("ta", "enakku panam thirumba venum"), ("te", "naaku dabbu tirigi kavali"),
                              ("bn", "amar taka ferot chai"), ("ta", "எனக்கு refund வேண்டும் please"),
                              ("te", "నాకు refund కావాలి"), ("bn", "আমার refund চাই")]:
            with self.subTest(message=message):
                result = self.parse(message)
                self.assertEqual(result["language"], code)
                self.assertFalse(result["needs_clarification"])
                self.assertIn("refund", [r["intent"] for r in result["requests"]])
        self.assertTrue(self.parse("எனக்கு refund வேண்டும் please")["mixed_language"])
        self.assertEqual(self.parse("আমার টাকা ফেরত চাই। অর্ডার ORD-101।")["detected_languages"], ["bn"])

    def test_entity_spelling_and_unicode_are_preserved(self):
        result = self.parse("My name is Zoë Nair; order: ORD-aBc-004; product code: SKU-rEfNd9; date: 2026-09-17. I need a refnud.")
        self.assertEqual(result["entities"], {"names": ["Zoë Nair"], "order_ids": ["ORD-aBc-004"], "product_codes": ["SKU-rEfNd9"], "dates": ["2026-09-17"]})
        self.assertTrue(result["spelling_interpretations"])
        self.assertIn("refund", [r["intent"] for r in result["requests"]])
        self.assertNotIn("SKU-rEfNd9", json.dumps(result["spelling_interpretations"]))

    def test_multiple_requests_and_ambiguous_followups(self):
        result = self.parse("Please track my delivery and explain the refund policy.")
        self.assertEqual({r["intent"] for r in result["requests"]}, {"order_status", "refund"})
        memory = {**empty_memory("en"), "intents": ["order_status", "refund"]}
        ambiguous = self.parse("And that one?", memory=memory)
        self.assertTrue(ambiguous["needs_clarification"])
        memory["intents"] = ["refund"]
        specific = self.parse("What about it?", memory=memory)
        self.assertFalse(specific["needs_clarification"])
        self.assertTrue(specific["requests"][0]["from_context"])

    def test_corrected_order_does_not_keep_negated_identifier(self):
        for message in ["Actually order ORD-22, not ORD-11.", "Correction: not ORD-11 but ORD-22."]:
            result = self.parse(message)
            self.assertEqual(result["entities"]["order_ids"], ["ORD-22"])
            self.assertEqual(result["kind"], "correction")

    def test_low_confidence_language_disabled_and_requested_switch(self):
        self.assertTrue(self.parse("zzzxv qqqrz")["needs_clarification"])
        self.assertTrue(self.parse("あれをお願いします")["needs_clarification"])
        self.assertTrue(self.parse("please do the thing")["needs_clarification"])
        self.assertEqual(self.parse("Please reply in Bengali.")["language"], "bn")
        self.parser = LanguageInterpreter(SessionConfig(languages=("en", "ta")))
        result = self.parse("আমার টাকা ফেরত চাই")
        self.assertTrue(result["needs_clarification"])
        self.assertIn("language_uncertain_or_disabled", result["clarification_reasons"])

    def test_unclear_second_request_and_known_name_are_not_silently_changed(self):
        result = self.parse("Explain refunds and do the thing.")
        self.assertTrue(result["needs_clarification"])
        self.assertIn("partial_intent_uncertain", result["clarification_reasons"])
        memory = empty_memory("en")
        memory["entities"]["names"] = ["Refunda"]
        self.assertEqual(self.parse("Refunda needs help", memory=memory)["spelling_interpretations"], [])


class SessionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        config = PipelineConfig()
        config.storage.source_dir = str(Path(self.tmp.name) / "docs")
        config.storage.state_dir = str(Path(self.tmp.name) / "state")
        self.pipeline = KnowledgeBasePipeline(config)
        self.workflow = self.pipeline.sessions
        self.client = self.workflow.start("admin-token", now=NOW)
        self.counter = 0

    def send(self, text="Please explain the refund policy for order ORD-123.", client=None, now=None, **changes):
        client = client or self.client
        self.counter += 1
        body = {"resume_key": client["resume_key"], "session_id": client["session"]["id"], "message_id": f"M-{self.counter}", "message": text, **changes}
        result = self.workflow.message(body, "admin-token", now)
        client["session"] = result["session"]
        return result

    def test_language_switches_within_same_session(self):
        a = self.send("எனக்கு பணம் திரும்ப வேண்டும்")
        b = self.send("నా ఆర్డర్ ORD-123 ఎక్కడ ఉంది?")
        c = self.send("আমার টাকা ফেরত চাই")
        self.assertEqual([a["interpretation"]["language"], b["interpretation"]["language"], c["interpretation"]["language"]], ["ta", "te", "bn"])
        self.assertEqual(a["session"]["id"], c["session"]["id"])
        self.assertIn("ORD-123", c["session"]["memory"]["entities"]["order_ids"])
        self.assertTrue(repr(c["reply"]))

    def test_explicit_language_preference_survives_english_followup(self):
        self.send("Please explain the refund policy.")
        switched = self.send("Please reply in Telugu.")
        self.assertEqual(switched["interpretation"]["language"], "te")
        next_turn = self.send("What about it?")
        self.assertEqual(next_turn["interpretation"]["language"], "te")
        self.assertIn("te", next_turn["session"]["memory"]["preferred_language"])

    def test_context_for_at_least_ten_messages_and_entity_correction(self):
        self.send("My name is Zoë Nair; order: ORD-123; product: SKU-Aa19; date: 2026-09-17. Please explain the refund policy.")
        for _ in range(10):
            result = self.send("Hello")
        self.assertEqual(len(result["session"]["history"]), 11)
        followup = self.send("What about it?")
        self.assertEqual(followup["interpretation"]["requests"][0]["intent"], "refund")
        self.assertEqual(followup["session"]["memory"]["entities"]["names"], ["Zoë Nair"])
        corrected = self.send("Actually order ORD-234, not ORD-123.")
        memory = corrected["session"]["memory"]
        self.assertEqual(memory["entities"]["order_ids"], ["ORD-234"])
        self.assertEqual(memory["entities"]["product_codes"], ["SKU-Aa19"])
        self.assertEqual(memory["corrections"][-1]["previous"], ["ORD-123"])

    def test_two_customers_are_isolated_and_ids_are_not_credentials(self):
        second = self.workflow.start("admin-token")
        one = self.send(entities={"names": ["Alice"], "order_ids": ["ORD-A"]})
        two = self.send("Please track my delivery", client=second, entities={"names": ["Bob"], "order_ids": ["ORD-B"]})
        self.assertNotIn("Alice", json.dumps(two))
        self.assertNotIn("ORD-A", json.dumps(two))
        self.assertNotIn("Bob", json.dumps(one))
        with self.assertRaises(AccessDenied):
            self.workflow.view(second["resume_key"], self.client["session"]["id"], "admin-token")
        with self.assertRaises(AccessDenied):
            self.workflow.start("service-token", self.client["resume_key"])

    def test_clarifying_a_selected_order_uses_prior_request(self):
        unclear = self.send("Please track order ORD-111 and ORD-222 delivery.")
        self.assertTrue(unclear["interpretation"]["needs_clarification"])
        selected = self.send("ORD-222")
        self.assertFalse(selected["interpretation"]["needs_clarification"])
        self.assertEqual(selected["session"]["memory"]["entities"]["order_ids"], ["ORD-222"])

    def test_expiry_exact_boundary_polling_does_not_extend(self):
        self.send()
        self.workflow.tick(NOW + timedelta(minutes=29, seconds=59))
        self.assertEqual(self.workflow.view(self.client["resume_key"], self.client["session"]["id"], "admin-token")["session"]["status"], "active")
        self.workflow.tick(NOW + timedelta(minutes=30))
        expired = self.workflow.view(self.client["resume_key"], self.client["session"]["id"], "admin-token")["session"]
        self.assertEqual(expired["status"], "expired")
        self.assertEqual(expired["ended_at"], (NOW + timedelta(minutes=30)).isoformat())
        self.assertEqual(expired["last_activity_at"], NOW.isoformat())

    def test_ordinary_message_resets_idle_timer(self):
        self.send()
        original = self.client["session"]["id"]
        result = self.send("எனக்கு பணம் திரும்ப வேண்டும்", now=NOW + timedelta(minutes=29))
        self.assertEqual(result["session"]["id"], original)
        self.workflow.tick(NOW + timedelta(minutes=30))
        viewed = self.workflow.view(self.client["resume_key"], original, "admin-token")["session"]
        self.assertEqual(viewed["status"], "active")
        self.assertEqual(viewed["idle_expires_at"], (NOW + timedelta(minutes=59)).isoformat())

    def test_return_at_24_hours_restores_summary_in_new_session(self):
        self.send(entities={"names": ["মালতী"], "order_ids": ["ORD-X1"]})
        old = self.client["session"]["id"]
        result = self.workflow.start("admin-token", self.client["resume_key"], NOW + timedelta(hours=24))
        self.assertNotEqual(result["session"]["id"], old)
        self.assertTrue(result["session"]["context_restored"])
        self.assertEqual(result["session"]["restored_summary"]["entities"]["names"], ["মালতী"])
        self.assertEqual(result["session"]["memory"]["intents"], ["refund"])

    def test_after_24_hours_is_fresh_and_old_private_context_is_purged(self):
        self.send(entities={"names": ["Unique Name"], "order_ids": ["ORD-X1"]})
        old = self.client["session"]["id"]
        result = self.workflow.start("admin-token", self.client["resume_key"], NOW + timedelta(hours=24, seconds=1))
        self.assertFalse(result["session"]["context_restored"])
        self.assertEqual(result["session"]["history"], [])
        self.assertEqual(result["session"]["summary"], {})
        self.assertEqual(result["session"]["memory"]["intents"], [])
        archived = self.workflow.view(self.client["resume_key"], old, "admin-token")["session"]
        self.assertTrue(archived["context_purged"])
        self.assertNotIn("Unique Name", json.dumps(archived))

    def test_late_message_automatically_rotates_and_rejects_old_session(self):
        self.send()
        old = self.client["session"]["id"]
        resumed = self.send("What about it?", now=NOW + timedelta(minutes=30))
        self.assertNotEqual(old, resumed["session"]["id"])
        self.assertTrue(resumed["session"]["context_restored"])
        self.assertFalse(resumed["interpretation"]["needs_clarification"])
        with self.assertRaises(AccessDenied):
            self.send("Hello", session_id=old)

    def test_idempotency_does_not_reset_idle_or_double_count(self):
        self.send(message_id="M-once")
        result = self.send(message_id="M-once", now=NOW + timedelta(minutes=29))
        self.assertTrue(result["replayed"])
        self.assertEqual(result["session"]["message_count"], 1)
        self.assertEqual(result["session"]["last_activity_at"], NOW.isoformat())
        with self.assertRaises(ValueError):
            self.send("Different request", message_id="M-once")

    def test_concurrent_same_ids_isolate_customers_and_deduplicate_retries(self):
        second = self.workflow.start("admin-token")
        alternate = KnowledgeBasePipeline(self.pipeline.config).sessions
        clients = [self.client, second]
        def submit(index):
            client = clients[index % 2]
            return (self.workflow if index % 3 else alternate).message({"resume_key": client["resume_key"],
                "session_id": client["session"]["id"], "message_id": "M-shared", "message": f"Please track order ORD-{index % 2 + 1} delivery"}, "admin-token")
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(submit, range(8)))
        self.assertEqual(sum(not r["replayed"] for r in results), 2)
        self.assertTrue(all(r["session"]["message_count"] == 1 for r in results))

    def test_configuration_validation_persistence_and_runtime_expiry(self):
        self.send()
        for values in ({"history_messages": 9}, {"language_threshold": 1.1}, {"inactivity_minutes": 0},
                       {"languages": ["zz"]}, {"intent_threshold": float("nan")}, {"restoration_hours": .1}):
            with self.assertRaises(ValueError):
                self.workflow.configure(values, "admin-token")
        self.workflow.tick(NOW + timedelta(minutes=12))
        self.workflow.configure({"inactivity_minutes": 10, "history_messages": 10, "intent_threshold": .95}, "admin-token")
        expired = self.workflow.view(self.client["resume_key"], self.client["session"]["id"], "admin-token")["session"]
        self.assertEqual(expired["status"], "expired")
        restart = KnowledgeBasePipeline(self.pipeline.config).sessions
        self.assertEqual(restart.settings("admin-token")["config"]["intent_threshold"], .95)
        with self.assertRaises(AccessDenied):
            self.workflow.configure({"history_messages": 10}, "service-token")

    def test_clarification_never_queries_and_entities_still_preserved(self):
        with patch.object(self.pipeline, "query") as query:
            result = self.send("zzzx qrrr", entities={"names": ["Zoë"], "order_ids": ["ORD-123"]})
            self.assertTrue(result["interpretation"]["needs_clarification"])
            query.assert_not_called()
        self.assertEqual(result["session"]["memory"]["entities"]["names"], ["Zoë"])

    def test_policy_answers_citations_and_scope_are_preserved(self):
        answer = QueryResult(answer="Refunds are available for 30 days. [S1]", confidence=1, contexts=[], active_version_id=None,
                             citations=[{"id": "S1", "quote": "Refunds are available for 30 days."}])
        with patch.object(self.pipeline, "query", return_value=answer) as query:
            result = self.send("எனக்கு refund வேண்டும்", product="Atlas Hub", region="US")
            followup = self.send("What about it?")
            self.assertEqual(query.call_args.kwargs["product"], "Atlas Hub")
            self.assertEqual(query.call_args.kwargs["region"], "US")
        self.assertIn(answer.answer, result["reply"])
        self.assertEqual(result["citations"], answer.citations)
        self.assertIn(answer.answer, followup["reply"])

    def test_historical_date_not_lost_when_query_is_interpreted(self):
        with patch.object(self.pipeline, "query", return_value=QueryResult(answer="No evidence", confidence=0, contexts=[], active_version_id=None)) as query:
            result = self.send("What was the refund policy on 2025-06-01?")
            self.assertEqual(query.call_args.kwargs["as_of"], "2025-06-01")
            self.assertEqual(result["session"]["memory"]["entities"]["dates"], ["2025-06-01"])
            self.send("What is the current refund policy?")
            self.assertIsNone(query.call_args.kwargs["as_of"])
            unclear = self.send("What was the refund policy on 06/07/2025?")
            self.assertTrue(unclear["interpretation"]["needs_clarification"])

    def test_safety_monitor_preserved_during_outage_and_session_expiry(self):
        with patch.object(self.pipeline, "query", side_effect=RuntimeError("private-key")):
            result = self.send("என் கணக்கு ஹேக் செய்யப்பட்டது. Please check this payment.")
        self.assertTrue(result["support"]["escalated"])
        self.assertEqual(result["answers"][0]["status"], "unavailable")
        self.workflow.tick(NOW + timedelta(minutes=31))
        conversations = self.pipeline.conversations.snapshot("admin-token")["conversations"]
        self.assertNotEqual(conversations[0]["status"], "resolved")
        self.assertNotIn("private-key", self.pipeline.metrics.path.read_text())

    def test_personal_details_private_but_not_in_support_logs(self):
        result = self.send("My name is Zoë Nair; order: ORD-123; I was charged twice. Email: zoe@example.com; card: 4111 1111 1111 1111")
        self.assertEqual(result["session"]["memory"]["entities"]["names"], ["Zoë Nair"])
        history = result["session"]["history"][0]["content"]
        self.assertNotIn("zoe@example.com", history)
        self.assertNotIn("4111 1111 1111 1111", history)
        support = json.dumps(self.pipeline.conversations.snapshot("admin-token"), ensure_ascii=False)
        self.assertNotIn("Zoë Nair", support)
        self.assertNotIn("Zoë Nair", self.pipeline.metrics.path.read_text())

    def test_http_access_time_controls_and_worker_expiry(self):
        server = create_server(self.pipeline, ("127.0.0.1", 0))
        thread = Thread(target=server.serve_forever, kwargs={"poll_interval": .02}, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join, 3)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_address[1]}"
        def call(path, data=None, token="admin-token"):
            req = Request(base + path, data=json.dumps(data).encode() if data is not None else None,
                          headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
            with urlopen(req, timeout=10) as response:
                return json.load(response)
        settings = call("/api/sessions/config")
        self.assertIn("ta", settings["config"]["languages"])
        for path, body in [("/api/sessions/start", {}), ("/api/sessions/config", {"config": {"inactivity_minutes": 1}}),
                           ("/api/sessions/tick", {"now": NOW.isoformat()})]:
            with self.assertRaises(HTTPError) as error:
                call(path, body, "viewer-token")
            self.assertEqual(error.exception.code, 403)
        self.send()
        self.pipeline.tickets.tick(NOW + timedelta(minutes=30))
        deadline = monotonic() + 4
        status = None
        while monotonic() < deadline:
            with self.workflow._transaction() as db:
                status = self.workflow._load(db, self.client["session"]["id"])["status"]
            if status == "expired":
                break
            sleep(.05)
        self.assertEqual(status, "expired")


if __name__ == "__main__":
    unittest.main()
