from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict
from datetime import timedelta
from pathlib import Path
from threading import RLock
import copy
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import uuid

from .config import SessionConfig
from .errors import AccessDenied
from .languages import ENTITY_KEYS, LANGUAGE_NAMES, LanguageInterpreter, REPLIES, safe_history
from .monitoring import Timer
from .tickets import timestamp


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def empty_memory(language):
    return {"language": language, "entities": {key: [] for key in ENTITY_KEYS}, "intents": [],
            "corrections": [], "ambiguous_entities": False, "pending_clarification": [], "scope": {}}


class SessionWorkflow:
    """Private customer context, separate from masked support operations and their unresolved timers."""

    def __init__(self, pipeline):
        self.pipeline = pipeline
        self.lock = RLock()
        self.path = Path(pipeline.config.storage.state_dir) / "sessions.sqlite3"
        with self._transaction() as db:
            db.execute("CREATE TABLE IF NOT EXISTS clients (id TEXT PRIMARY KEY, owner TEXT NOT NULL, key_hash TEXT UNIQUE NOT NULL, active_session TEXT, last_customer_at TEXT)")
            db.execute("CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, client_id TEXT NOT NULL, payload TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS session_requests (client_id TEXT, message_id TEXT, fingerprint TEXT, payload TEXT, PRIMARY KEY(client_id,message_id))")
            db.execute("CREATE TABLE IF NOT EXISTS session_settings (id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL, payload TEXT NOT NULL)")
            db.execute("INSERT OR IGNORE INTO session_settings VALUES (1,1,?)", (json.dumps(asdict(pipeline.config.sessions)),))
            self._policy(db)

    @contextmanager
    def _transaction(self):
        with self.lock:
            db = sqlite3.connect(self.path, timeout=20)
            db.row_factory = sqlite3.Row
            try:
                db.execute("BEGIN IMMEDIATE")
                yield db
                db.commit()
            except Exception:
                db.rollback()
                raise
            finally:
                db.close()

    def _policy(self, db):
        row = db.execute("SELECT revision,payload FROM session_settings WHERE id=1").fetchone()
        config = SessionConfig(**json.loads(row["payload"]))
        self.pipeline.config.sessions = config
        return config, row["revision"]

    def _now(self, token=None, now=None, internal=False):
        if not internal:
            self.pipeline.access.authorize(token, "ticket_write")
        if now is not None and not internal:
            self.pipeline.access.authorize(token, "ticket_admin")
        with self.pipeline.tickets._transaction() as db:
            return self.pipeline.tickets._now(db, now)

    def _client(self, db, key, token):
        if not isinstance(key, str) or len(key) > 200:
            raise AccessDenied("Invalid session credentials")
        client = db.execute("SELECT * FROM clients WHERE key_hash=?", (digest(key),)).fetchone()
        if client is None or not hmac.compare_digest(client["owner"], digest(token)):
            raise AccessDenied("Invalid session credentials")
        return client

    def _load(self, db, identifier):
        row = db.execute("SELECT payload FROM sessions WHERE id=?", (identifier,)).fetchone()
        return json.loads(row[0]) if row else None

    def _save(self, db, session):
        db.execute("INSERT OR REPLACE INTO sessions VALUES (?,?,?)", (session["id"], session["client_id"], json.dumps(session)))

    def _expire(self, db, session, now, config):
        idle_at = timestamp(session["last_activity_at"]) + timedelta(minutes=config.inactivity_minutes)
        if session["status"] == "active" and now >= idle_at:
            session.update({"status": "expired", "ended_at": idle_at.isoformat(), "end_reason": "inactivity"})
        if session["status"] == "expired" and now > timestamp(session["last_activity_at"]) + timedelta(hours=config.restoration_hours):
            session.update({"memory": empty_memory(config.default_language), "history": [], "summary": {},
                            "restored_summary": None, "context_purged": True})
            db.execute("DELETE FROM session_requests WHERE client_id=? AND json_extract(payload, '$.session.id')=?", (session["client_id"], session["id"]))
        session["idle_expires_at"] = idle_at.isoformat()
        session["restore_until"] = (timestamp(session["last_activity_at"]) + timedelta(hours=config.restoration_hours)).isoformat()
        session["history"] = session["history"][-config.history_messages:]
        self._save(db, session)

    def _open(self, db, client, now, config):
        old = self._load(db, client["active_session"]) if client["active_session"] else None
        if old:
            if now < timestamp(old["last_activity_at"]):
                raise ValueError("Session time cannot move backwards")
            self._expire(db, old, now, config)
            if old["status"] == "active":
                return old
        restored = bool(old and not old.get("context_purged") and client["last_customer_at"] and
                        now <= timestamp(client["last_customer_at"]) + timedelta(hours=config.restoration_hours))
        session = {"id": "S-" + uuid.uuid4().hex, "client_id": client["id"], "status": "active",
                   "created_at": now.isoformat(), "last_activity_at": now.isoformat(), "ended_at": None,
                   "message_count": 0, "memory": copy.deepcopy(old["memory"]) if restored else empty_memory(config.default_language),
                   "history": copy.deepcopy(old["history"][-config.history_messages:]) if restored else [],
                   "summary": copy.deepcopy(old["summary"]) if restored else {},
                   "context_restored": restored, "restored_from": old["id"] if restored else None,
                   "restored_summary": copy.deepcopy(old["summary"]) if restored else None}
        self._expire(db, session, now, config)
        db.execute("UPDATE clients SET active_session=? WHERE id=?", (session["id"], client["id"]))
        return session

    def _public(self, session):
        result = copy.deepcopy(session)
        result.pop("client_id", None)
        return result

    def start(self, token=None, resume_key=None, now=None):
        current = self._now(token, now)
        with self._transaction() as db:
            config, revision = self._policy(db)
            issued = None
            if resume_key is None:
                issued = secrets.token_urlsafe(32)
                db.execute("INSERT INTO clients VALUES (?,?,?,?,NULL)", ("U-" + uuid.uuid4().hex, digest(token), digest(issued), None))
                client = self._client(db, issued, token)
            else:
                client = self._client(db, resume_key, token)
            session = self._open(db, client, current, config)
            result = {"session": self._public(session), "config_revision": revision, "clock": current.isoformat()}
            if issued:
                result["resume_key"] = issued
            return result

    def view(self, resume_key, session_id, token=None):
        current = self._now(token)
        with self._transaction() as db:
            config, _ = self._policy(db)
            client = self._client(db, resume_key, token)
            session = self._load(db, session_id)
            if not session or session["client_id"] != client["id"]:
                raise AccessDenied("Invalid session credentials")
            self._expire(db, session, current, config)
            return {"session": self._public(session), "clock": current.isoformat()}

    def message(self, payload, token=None, now=None):
        timer = Timer()
        self.pipeline.access.authorize(token, "ticket_write")
        if not isinstance(payload, dict) or len(json.dumps(payload)) > 60000:
            raise ValueError("A session message object of at most 60 KB is required")
        message, request_id = payload.get("message"), payload.get("message_id")
        if not isinstance(message, str) or not message.strip() or len(message) > 12000:
            raise ValueError("Message must contain between 1 and 12000 characters")
        if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", request_id):
            raise ValueError("An opaque message_id is required for retries")
        if self.pipeline.injection.scan(message + " " + json.dumps(payload.get("entities", {}), ensure_ascii=False)).suspicious:
            raise ValueError("Unsafe instructions in session content")
        fingerprint = digest(json.dumps({k: payload.get(k) for k in ("session_id", "message", "language", "entities", "product", "region", "as_of")}, sort_keys=True))
        current = self._now(token, now)
        with self._transaction() as db:
            config, revision = self._policy(db)
            client = self._client(db, payload.get("resume_key"), token)
            latest = self._load(db, client["active_session"])
            self._expire(db, latest, current, config)
            previous = db.execute("SELECT fingerprint,payload FROM session_requests WHERE client_id=? AND message_id=?", (client["id"], request_id)).fetchone()
            if previous:
                if previous["fingerprint"] != fingerprint:
                    raise ValueError("Message ID already used for different content")
                result = json.loads(previous["payload"])
                saved = self._load(db, result["session"]["id"])
                self._expire(db, saved, current, config)
                result["session"] = self._public(saved)
                result["replayed"] = True
                return result
            if payload.get("session_id") != client["active_session"]:
                raise AccessDenied("Session is not current for these credentials; resume before sending")
            session = self._open(db, client, current, config)
            analysis = LanguageInterpreter(config).interpret(message, session["memory"], payload.get("language", "auto"), payload.get("entities"))
            memory = session["memory"]
            if analysis["preferred_language"] in config.languages:
                memory["preferred_language"] = analysis["preferred_language"]
            for key in ("product", "region", "as_of"):
                value = payload.get(key)
                if value is not None:
                    if not isinstance(value, str) or len(value) > 200:
                        raise ValueError("Product, region and policy date must be text of at most 200 characters")
                    memory.setdefault("scope", {})[key] = value or None
            if analysis["policy_date"]:
                if payload.get("as_of") and payload["as_of"] != analysis["policy_date"]:
                    analysis["needs_clarification"] = True
                    analysis["clarification_reasons"].append("conflicting_policy_dates")
                else:
                    memory.setdefault("scope", {})["as_of"] = analysis["policy_date"]
            elif analysis["current_policy"]:
                memory.setdefault("scope", {})["as_of"] = None
            # Only explicit entities may overwrite facts. Never typo-correct or translate customer identifiers.
            for key, values in analysis["entities"].items():
                if values:
                    old = memory["entities"][key]
                    if old and old != values:
                        memory["corrections"].append({"field": key, "previous": old, "current": values, "at": current.isoformat(), "explicit": analysis["correcting"]})
                    memory["entities"][key] = values
            memory["corrections"] = memory["corrections"][-50:]
            memory["ambiguous_entities"] = any(len(memory["entities"][key]) > 1 for key in ("order_ids", "product_codes"))
            if not analysis["needs_clarification"] or analysis["language_confidence"] >= config.language_threshold:
                memory["language"] = analysis["language"]
            if analysis["requests"]:
                memory["intents"] = [r["intent"] for r in analysis["requests"]]
            memory["pending_clarification"] = analysis["clarification_reasons"]
            names = memory["entities"]["names"]
            support_message = message
            known_names = set(names)
            for correction in memory["corrections"]:
                if correction["field"] == "names":
                    known_names.update(correction["previous"])
            for name in sorted(known_names, key=len, reverse=True):
                support_message = support_message.replace(name, "[CUSTOMER]")
            monitored = self.pipeline.conversations.receive({"conversation_id": "C-" + session["id"],
                "message_id": request_id, "message": support_message, "language": analysis["language"]}, token)
            reply, answers, citations = self._answer(analysis, memory.get("scope", {}), token, current)
            session["history"].append({"message_id": request_id, "content": safe_history(message, memory["entities"]),
                                       "at": current.isoformat(), "language": analysis["language"], "intents": [r["intent"] for r in analysis["requests"]],
                                       "reply": reply, "interpretation": analysis, "citations": citations})
            session["history"] = session["history"][-config.history_messages:]
            session["message_count"] += 1
            session["last_activity_at"] = current.isoformat()
            session["summary"] = {"entities": copy.deepcopy(memory["entities"]), "requests": list(memory["intents"]),
                                  "scope": copy.deepcopy(memory.get("scope", {})),
                                  "language": memory["language"], "clarification_needed": list(memory["pending_clarification"]),
                                  "recent_messages": [m["content"] for m in session["history"][-10:]]}
            self._expire(db, session, current, config)
            db.execute("UPDATE clients SET last_customer_at=? WHERE id=?", (current.isoformat(), client["id"]))
            result = {"session": self._public(session), "interpretation": analysis, "reply": reply, "answers": answers,
                      "citations": citations, "support": {key: monitored[key] for key in ("id", "queue", "escalated", "tone")},
                      "config_revision": revision, "replayed": False}
            db.execute("INSERT INTO session_requests VALUES (?,?,?,?)", (client["id"], request_id, fingerprint, json.dumps(result)))
        self.pipeline.metrics.record("session_message", latency_ms=round(timer.elapsed_ms, 3), language=analysis["language"],
                                     language_confidence=analysis["language_confidence"], intent_confidence=analysis["intent_confidence"],
                                     clarification=analysis["needs_clarification"], request_count=len(analysis["requests"]))
        return result

    def _answer(self, analysis, payload, token, now):
        phrases = REPLIES[analysis["language"]]
        if analysis["needs_clarification"]:
            key = "language" if "language_uncertain_or_disabled" in analysis["clarification_reasons"] else "clarify"
            return phrases[key], [], []
        if analysis["kind"] in {"correction", "greeting", "language_switch"}:
            key = {"correction": "updated", "greeting": "greeting", "language_switch": "switched"}[analysis["kind"]]
            return phrases[key], [], []
        answers, citations = [], []
        for request in analysis["requests"]:
            try:
                result = self.pipeline.query(request["query"], token=token, now=now,
                    product=payload.get("product"), region=payload.get("region"), as_of=payload.get("as_of")).to_dict()
                if result["status"] == "answered":
                    mapping = {c["id"]: f"S{len(citations) + index + 1}" for index, c in enumerate(result["citations"])}
                    text = re.sub(r"\[(S\d+)\]", lambda m: "[" + mapping[m[1]] + "]", result["answer"])
                    sources = [{**c, "id": mapping[c["id"]]} for c in result["citations"]]
                    citations.extend(sources)
                    text = phrases["source"] + "\n" + text
                else:
                    text, sources = phrases["missing"], []
                answers.append({"intent": request["intent"], "status": result["status"], "text": text, "citations": sources})
            except Exception as exc:
                self.pipeline.metrics.record("session_answer_failure", error=type(exc).__name__)
                answers.append({"intent": request["intent"], "status": "unavailable", "text": phrases["unavailable"], "citations": []})
        return "\n\n".join(f"{index + 1}. {a['text']}" if len(answers) > 1 else a["text"] for index, a in enumerate(answers)), answers, citations

    def tick(self, now=None):
        current = self._now(now=now, internal=True)
        with self._transaction() as db:
            config, _ = self._policy(db)
            for row in db.execute("SELECT payload FROM sessions").fetchall():
                self._expire(db, json.loads(row[0]), current, config)

    def settings(self, token=None):
        self.pipeline.access.authorize(token, "ticket_read")
        with self.pipeline.tickets._transaction() as clock_db:
            clock = {"mode": "simulated" if self.pipeline.tickets._meta(clock_db, "clock") else "live",
                     "now": self.pipeline.tickets._now(clock_db).isoformat()}
        with self._transaction() as db:
            config, revision = self._policy(db)
            return {"config": asdict(config), "revision": revision, "available_languages": LANGUAGE_NAMES, "clock": clock}

    def configure(self, changes, token=None):
        self.pipeline.access.authorize(token, "ticket_admin")
        if not isinstance(changes, dict) or not set(changes) <= set(asdict(SessionConfig())):
            raise ValueError("Unknown session settings")
        with self._transaction() as db:
            config, revision = self._policy(db)
            config = SessionConfig(**{**asdict(config), **changes})
            db.execute("UPDATE session_settings SET revision=?,payload=? WHERE id=1", (revision + 1, json.dumps(asdict(config))))
        self.tick()
        return self.settings(token)
