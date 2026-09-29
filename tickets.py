from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from threading import RLock
from typing import Any, Mapping
from zoneinfo import ZoneInfo
import hashlib
import json
import math
import re
import sqlite3
import uuid

from .config import PipelineConfig, TicketConfig
from .evidence import EvidenceAnalyzer
from .security import AccessController, PromptInjectionProtector, SensitiveDataMasker


UTC = timezone.utc
CATEGORIES = {"shipping", "billing", "technical", "product", "general"}
FIELDS = {"customer", "contact", "order", "product", "issue", "evidence"}
PRIORITIES = ("P1", "P2", "P3", "P4")


def timestamp(value: Any = None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)).astimezone(UTC)


def normalized(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.casefold()))


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def local_zone(name: str):
    if name == "UTC":
        return UTC
    offset = re.fullmatch(r"([+-])(\d{2}):(\d{2})", name)
    if offset:
        hours, minutes = int(offset[2]), int(offset[3])
        if hours > 23 or minutes > 59:
            raise ValueError("Invalid timezone offset")
        return timezone(timedelta(minutes=(hours * 60 + minutes) * (1 if offset[1] == "+" else -1)))
    try:
        return ZoneInfo(name)
    except (KeyError, ValueError) as exc:
        raise ValueError("Unknown timezone; use UTC, an installed IANA zone, or an offset such as +05:30") from exc


class BusinessCalendar:
    def __init__(self, config: TicketConfig, overrides: Mapping[str, Any] | None = None):
        overrides = overrides or {}
        self.zone = local_zone(str(overrides.get("timezone", config.timezone)))
        self.start = time.fromisoformat(str(overrides.get("business_start", config.business_start)))
        self.end = time.fromisoformat(str(overrides.get("business_end", config.business_end)))
        self.weekends = set(config.weekends)
        self.holidays = {date.fromisoformat(day) for day in config.holidays}
        if self.start >= self.end or self.start.tzinfo or self.end.tzinfo:
            raise ValueError("Business hours must be a same-day interval with start before end")
        if not self.weekends < set(range(7)):
            raise ValueError("Weekend days must be 0-6 and leave at least one working day")

    def interval(self, day: date):
        if day.weekday() in self.weekends or day in self.holidays:
            return None
        return (datetime.combine(day, self.start, self.zone).astimezone(UTC),
                datetime.combine(day, self.end, self.zone).astimezone(UTC))

    def is_open(self, now: datetime) -> bool:
        interval = self.interval(now.astimezone(self.zone).date())
        return bool(interval and interval[0] <= now < interval[1])

    def minutes_between(self, start: datetime, end: datetime) -> float:
        if end <= start:
            return 0.0
        day, last = start.astimezone(self.zone).date(), end.astimezone(self.zone).date()
        if (last - day).days > 36600:
            raise ValueError("SLA interval exceeds 100 years")
        seconds = 0.0
        while day <= last:
            interval = self.interval(day)
            if interval:
                seconds += max(0.0, (min(end, interval[1]) - max(start, interval[0])).total_seconds())
            day += timedelta(days=1)
        return seconds / 60

    def add_minutes(self, start: datetime, minutes: float) -> datetime:
        remaining = minutes * 60
        day = start.astimezone(self.zone).date()
        for _ in range(36600):
            interval = self.interval(day)
            if interval:
                cursor = max(start, interval[0])
                available = max(0.0, (interval[1] - cursor).total_seconds())
                if available and remaining <= available:
                    return cursor + timedelta(seconds=remaining)
                remaining -= available
            day += timedelta(days=1)
        raise ValueError("SLA deadline exceeds calendar limit")


def validate_config(config: TicketConfig) -> None:
    BusinessCalendar(config)
    if not set(config.mandatory_fields) <= FIELDS:
        raise ValueError("Unknown mandatory ticket field")
    if not set(config.required_by_category) <= CATEGORIES or any(
        not set(values) <= FIELDS for values in config.required_by_category.values()
    ):
        raise ValueError("Unknown category or required field")
    if set(config.sla_minutes) != set(PRIORITIES) or any(
        isinstance(value, bool) or not math.isfinite(float(value)) or not 0 < float(value) <= 525600
        for value in config.sla_minutes.values()
    ):
        raise ValueError("Each priority requires a positive SLA in business minutes (maximum one year)")
    if not math.isfinite(config.duplicate_window_hours) or not 0 < config.duplicate_window_hours <= 8760:
        raise ValueError("Duplicate window must be between 0 and 8760 hours")
    teams = {team["id"] for team in config.teams}
    if len(teams) != len(config.teams) or config.escalation_team not in teams:
        raise ValueError("Teams need unique IDs and an existing escalation team")
    if len({agent["id"] for agent in config.agents}) != len(config.agents):
        raise ValueError("Agent IDs must be unique")
    for team in config.teams:
        BusinessCalendar(config, team)
    for item in [*config.teams, *config.agents]:
        if not isinstance(item.get("available"), bool) or not set(item.get("skills", [])) <= CATEGORIES:
            raise ValueError("Availability must be boolean and skills must be known categories")
    for agent in config.agents:
        if agent["team"] not in teams or type(agent.get("capacity")) is not int or not 0 <= agent["capacity"] <= 10000:
            raise ValueError("Agents require an existing team and a nonnegative integer capacity")


def classify(issue: str) -> tuple[str, str]:
    rules = [
        ("billing", "duplicate_charge", r"charged twice|billed twice|double[- ]charged|duplicate charge"),
        ("billing", "refund", r"refund|money back"),
        ("shipping", "delivery_delay", r"not (?:yet )?(?:arrived|delivered)|hasn't arrived|has not arrived|late delivery|late shipment|delayed|missing (?:order|parcel)|where is my order"),
        ("shipping", "delivery", r"shipping|delivery|parcel|tracking|shipment"),
        ("technical", "login", r"log\s?in|sign\s?in|password|locked out|authentication"),
        ("technical", "outage", r"outage|service down|offline|unavailable"),
        ("technical", "error", r"error|crash|bug|does not work|not working"),
        ("product", "damage", r"damaged|broken|defect|overheat|unsafe|injur"),
        ("billing", "payment", r"payment|invoice|bill|charge"),
    ]
    for category, intent, pattern in rules:
        if re.search(pattern, issue, re.I):
            return category, intent
    return "general", "general"


def labeled(text: str, label: str) -> str:
    match = re.search(rf"(?:^|[\n;])\s*(?:{label})\s*[:=]\s*([^\n;]+)", text, re.I)
    return match[1].strip() if match else ""


def order_in_text(text: str) -> str:
    token = re.search(r"\b(?:ORD|ORDER|PO)-[A-Z0-9-]+\b", text, re.I)
    if token:
        return token[0]
    label = re.search(r"\border\s*(?:id|number|no\.?|#)?\s*[:#-]?\s*([A-Z0-9-]{3,})\b", text, re.I)
    return label[1] if label and any(char.isdigit() for char in label[1]) else ""


def extract_issues(text: str) -> list[str]:
    explicit = re.findall(r"(?:^|\n)\s*(?:issue|problem)(?:\s+\d+)?\s*:\s*([^\n]+)", text, re.I)
    if explicit:
        chunks = explicit
    else:
        chunks = [line for line in text.splitlines() if not re.match(
            r"\s*(customer|name|contact|email|phone|order|product|sku|address|card|severity|impact)\s*(?:id)?\s*:", line, re.I
        )]
    parts = []
    for chunk in chunks:
        parts.extend(re.split(r"\s*(?:;|\balso\b|\bseparately\b|\bunrelated issue\b|\band\s+(?=(?:I |my |the )?(?:cannot|can't|was charged|am unable)))\s*", chunk, flags=re.I))
    return [part.strip(" .,-") for part in parts if part.strip(" .,-")][:20]


class TicketWorkflow:
    """Transactional ticket intake, routing, and a restart-safe notification outbox."""

    def __init__(self, config: PipelineConfig, access: AccessController, evidence: EvidenceAnalyzer):
        self.config, self.access, self.evidence = config, access, evidence
        self.masker, self.injection = SensitiveDataMasker(), PromptInjectionProtector()
        self.lock = RLock()
        self.path = Path(config.storage.state_dir) / "tickets.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        validate_config(config.tickets)
        with self._transaction() as db:
            db.execute("CREATE TABLE IF NOT EXISTS tickets (id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, payload TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, ticket_id TEXT, kind TEXT, at TEXT, payload TEXT, dedupe TEXT UNIQUE)")
            if self._meta(db, "policy") is None:
                self._set_meta(db, "policy", {"revision": 1, "config": asdict(config.tickets)})
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

    def _meta(self, db, key):
        row = db.execute("SELECT payload FROM metadata WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def _set_meta(self, db, key, value):
        db.execute("INSERT OR REPLACE INTO metadata VALUES (?, ?)", (key, json.dumps(value)))

    def _policy(self, db):
        policy = self._meta(db, "policy")
        self.config.tickets = TicketConfig(**policy["config"])
        return policy["revision"]

    def _now(self, db, now=None):
        clock = self._meta(db, "clock")
        if now is not None:
            current = timestamp(now)
            if clock and current < timestamp(clock["now"]):
                raise ValueError("The simulated ticket clock cannot move backwards")
            self._set_meta(db, "clock", {"mode": "simulated", "now": current.isoformat()})
            return current
        return timestamp(clock["now"]) if clock else timestamp()

    def _all(self, db):
        return [json.loads(row[0]) for row in db.execute("SELECT payload FROM tickets ORDER BY id")]

    def _save(self, db, ticket):
        db.execute("INSERT OR REPLACE INTO tickets VALUES (?, ?)", (ticket["id"], json.dumps(ticket)))

    def _event(self, db, ticket, kind, now, payload=None, once=False):
        db.execute("INSERT OR IGNORE INTO events(ticket_id, kind, at, payload, dedupe) VALUES (?, ?, ?, ?, ?)", (
            ticket["id"], kind, now.isoformat(), json.dumps(payload or {}),
            ticket["id"] + ":" + kind if once else None,
        ))

    def _mask(self, value: str, customer: str = "") -> str:
        text = value
        if customer:
            text = re.sub(re.escape(customer), "[CUSTOMER]", text, flags=re.I)
        text = re.sub(r"(?im)\b(address|card|cvv|cvc|account number)\s*[:=]\s*[^\n;]+", r"\1: [REDACTED]", text)
        return self.masker.mask(text)[0]

    def _public(self, ticket):
        output = json.loads(json.dumps(ticket))
        customer = output.get("customer", "")
        output["customer"] = "[CUSTOMER]" if customer else ""
        output["contact"] = "[CONTACT]" if output.get("contact") else ""
        output.pop("identity", None)
        output.pop("fingerprint", None)
        for key in ("issue", "order", "product", "handoff_summary"):
            output[key] = self._mask(output.get(key, ""), customer)
        output["conversation_ids"] = [self._mask(value, customer) for value in output["conversation_ids"]]
        return output

    def _fingerprint(self, identity, category, intent, fields, issue):
        error = re.search(r"\b(?:ERR|E|HTTP)[-_]?\d{2,8}\b", issue, re.I)
        signature = intent if intent in {"duplicate_charge", "refund", "delivery_delay", "login", "outage"} else normalized(issue)
        subject = {key: normalized(fields[key]) for key in ("order", "product")}
        return digest(json.dumps([identity, category, subject, signature, error[0].upper() if error else ""]))

    def _snapshot(self, db, now):
        tickets = self._all(db)
        events = [dict(row) for row in db.execute("SELECT id,ticket_id,kind,at,payload FROM events ORDER BY id DESC LIMIT 200")]
        for event in events:
            event["payload"] = json.loads(event["payload"])
        return {
            "tickets": [self._public(ticket) for ticket in sorted(tickets, key=lambda t: (t["priority"], t["created_at"]))],
            "events": events, "config": asdict(self.config.tickets),
            "policy_revision": self._meta(db, "policy")["revision"],
            "clock": {"mode": "simulated" if self._meta(db, "clock") else "live", "now": now.isoformat()},
            "counts": {"open": sum(t["status"] != "resolved" for t in tickets),
                       "warning": sum(t["status"] != "resolved" and t["sla"]["state"] == "warning" for t in tickets),
                       "breached": sum(t["status"] != "resolved" and t["sla"]["state"] == "breached" for t in tickets),
                       "unassigned": sum(t["status"] != "resolved" and not t["agent_id"] for t in tickets)},
        }

    def snapshot(self, token=None):
        self.access.authorize(token, "ticket_read")
        with self._transaction() as db:
            self._policy(db)
            return self._snapshot(db, self._now(db))

    def convert(self, conversation: Mapping[str, Any], token=None, now=None):
        self.access.authorize(token, "ticket_write")
        if not isinstance(conversation, Mapping):
            raise ValueError("Conversation must be an object")
        if conversation.get("unresolved", True) is not True:
            return {"status": "resolved_conversation", "tickets": [], "created": 0, "duplicates": 0}
        messages = conversation.get("messages", [])
        if not isinstance(messages, list) or any(not isinstance(item, Mapping) for item in messages):
            raise ValueError("Messages must be a list of role/content objects")
        text = str(conversation.get("message", "")) or "\n".join(
            str(item.get("content", "")) for item in messages if item.get("role") in {"customer", "user"}
        )
        if len(json.dumps(conversation)) > 100000:
            raise ValueError("Conversation is too large")
        if self.injection.scan(json.dumps(conversation)).score >= self.config.security.prompt_injection_threshold:
            raise ValueError("Conversation contains unsafe instructions")
        with self._transaction() as db:
            revision = self._policy(db)
            current = self._now(db, now)
            started = timestamp(conversation.get("started_at", current))
            if started > current:
                raise ValueError("Conversation start cannot be in the future")
            customer = str(conversation.get("customer") or labeled(text, "customer|name"))
            email = self.masker.EMAIL.search(text)
            phone = self.masker.PHONE.search(text)
            contact = str(conversation.get("contact") or labeled(text, "contact|email|phone") or (email[0] if email else phone[0] if phone else ""))
            common = {"customer": customer, "contact": contact,
                      "order": str(conversation.get("order") or labeled(text, "order(?: id| number| #)?")),
                      "product": str(conversation.get("product") or labeled(text, "product|sku"))}
            if not common["order"]:
                common["order"] = order_in_text(text)
            refs = conversation.get("file_refs", [])
            evidence, evidence_questions, evidence_values = self._evidence(refs, current)
            for key, values in evidence_values.items():
                if not common[key] and len(values) == 1:
                    common[key] = values[0]
                elif len(values) > 1 or (values and common[key] and normalized(common[key]) not in {normalized(v) for v in values}):
                    evidence_questions.append(f"Confirm {key}: the conversation and evidence contain different values.")
            raw_issues = conversation.get("issues")
            if raw_issues is None:
                raw_issues = [{"issue": part} for part in extract_issues(text)] or [{"issue": ""}]
            if not isinstance(raw_issues, list) or not 1 <= len(raw_issues) <= 20:
                raise ValueError("Provide between 1 and 20 issues")
            conversation_id = str(conversation.get("conversation_id") or "conversation-" + uuid.uuid4().hex[:12])
            identity = digest(str(conversation.get("customer_id") or contact.casefold().strip() or conversation_id))
            tickets = self._all(db)
            selected, created, duplicates = [], 0, 0
            for raw in raw_issues:
                if isinstance(raw, str):
                    raw = {"issue": raw}
                if not isinstance(raw, Mapping):
                    raise ValueError("Each issue must be text or an object")
                fields = {key: str(raw.get(key, value)) for key, value in common.items()}
                issue = str(raw.get("issue", ""))
                local_order = order_in_text(issue)
                if local_order and "order" not in raw:
                    fields["order"] = local_order
                category, intent = classify(issue)
                category = str(raw.get("category", category))
                if category not in CATEGORIES:
                    raise ValueError("Unknown issue category")
                clean_issue = self._mask(issue, customer)
                # Broad topic matches only group tickets; deduplication also needs the same intent and subject.
                fingerprint = self._fingerprint(identity, category, intent, fields, clean_issue)
                duplicate = next((t for t in tickets if self._is_duplicate(t, identity, conversation_id, category, intent, fields, clean_issue, fingerprint) and
                                  t["status"] != "resolved" and current - timestamp(t["created_at"]) <= timedelta(hours=self.config.tickets.duplicate_window_hours)), None)
                severity = str(raw.get("severity", conversation.get("severity", ""))).lower()
                if not severity:
                    severity = "critical" if re.search(r"injur|unsafe|data loss|security breach|service down|outage", issue, re.I) else "high" if re.search(r"blocked|cannot|can't|broken", issue, re.I) else "medium"
                if severity not in {"low", "medium", "high", "critical"}:
                    raise ValueError("Severity must be low, medium, high or critical")
                sentiment = str(raw.get("sentiment", conversation.get("sentiment", ""))).lower()
                if not sentiment:
                    sentiment = "angry" if re.search(r"furious|angry|unacceptable", text, re.I) else "negative" if re.search(r"frustrat|disappoint|upset", text, re.I) else "neutral"
                if sentiment not in {"positive", "neutral", "negative", "angry"}:
                    raise ValueError("Unknown sentiment")
                impact = raw.get("customer_impact", conversation.get("customer_impact", 1))
                if type(impact) is not int or not 1 <= impact <= 1000000000:
                    raise ValueError("Customer impact must be a positive count")
                if duplicate:
                    duplicates += 1
                    duplicate["duplicate_count"] += 1
                    duplicate["conversation_ids"] = sorted(set(duplicate["conversation_ids"] + [conversation_id]))
                    duplicate["evidence"] = list({e["file_id"]: e for e in [*duplicate["evidence"], *evidence]}.values())
                    duplicate["evidence_questions"] = list(dict.fromkeys(duplicate["evidence_questions"] + evidence_questions))
                    for key, value in fields.items():
                        if not duplicate.get(key):
                            duplicate[key] = value
                    duplicate["customer_impact"] = max(duplicate["customer_impact"], impact)
                    severities = ["low", "medium", "high", "critical"]
                    duplicate["severity"] = max((duplicate["severity"], severity), key=severities.index)
                    duplicate["sentiment"] = sentiment
                    duplicate["started_at"] = min(timestamp(duplicate["started_at"]), started).isoformat()
                    self._event(db, duplicate, "duplicate_request", current)
                    selected.append(duplicate)
                    continue
                related = next((t for t in tickets if t["identity"] == identity and t["category"] == category and
                                t["status"] != "resolved" and any(fields[k] and normalized(fields[k]) == normalized(t[k]) for k in ("order", "product"))), None)
                ticket = {"id": "T-" + uuid.uuid4().hex[:10].upper(), **fields,
                          "issue": clean_issue, "category": category, "intent": intent,
                          "identity": identity, "fingerprint": fingerprint,
                          "evidence": evidence, "evidence_questions": list(evidence_questions),
                          "conversation_ids": [conversation_id], "duplicate_count": 0,
                          "created_at": current.isoformat(), "started_at": started.isoformat(),
                          "severity": severity, "sentiment": sentiment, "customer_impact": impact,
                          "status": "queued", "agent_id": None, "team_id": None, "routing_reason": "",
                          "group_id": related["group_id"] if related else "G-" + uuid.uuid4().hex[:10],
                          "escalated_at": None, "resolved_at": None, "sla_priority": "", "sla": {}}
                self._priority(ticket, current, 0)
                ticket["sla_priority"] = ticket["priority"]
                tickets.append(ticket)
                selected.append(ticket)
                created += 1
                self._event(db, ticket, "ticket_created", current, {"category": category})
            self._refresh(db, tickets, current, revision)
            return {"status": "needs_information" if any(t["missing_fields"] or t["evidence_questions"] for t in selected) else "duplicate" if not created else "created",
                    "created": created, "duplicates": duplicates,
                    "tickets": [self._public(t) for t in {t["id"]: t for t in selected}.values()],
                    "clock": current.isoformat()}

    def _is_duplicate(self, ticket, identity, conversation_id, category, intent, fields, issue, fingerprint):
        if ticket["fingerprint"] == fingerprint:
            return True
        if ticket["identity"] != identity and conversation_id not in ticket["conversation_ids"]:
            return False
        if category != ticket["category"] or intent != ticket["intent"]:
            return False
        if any(fields[key] and ticket[key] and normalized(fields[key]) != normalized(ticket[key]) for key in ("order", "product")):
            return False
        anchor = any(fields[key] and normalized(fields[key]) == normalized(ticket[key]) for key in ("order", "product"))
        if not anchor and conversation_id not in ticket["conversation_ids"]:
            return False
        return self._fingerprint(identity, category, intent, fields, issue) == self._fingerprint(identity, category, intent, fields, ticket["issue"])

    def _evidence(self, refs, now):
        if not isinstance(refs, list) or len(refs) > 20:
            raise ValueError("Evidence must be a list of up to 20 uploaded file IDs")
        records, questions, values = [], [], {"order": [], "product": []}
        for ref in dict.fromkeys(refs):
            if not isinstance(ref, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", ref):
                raise ValueError("Evidence must reference uploaded file IDs")
            # Only managed uploads may be attached, never a caller-supplied filesystem path.
            if not (self.evidence.store.metadata_dir / (ref + ".json")).is_file():
                questions.append("An evidence file is missing or expired. Please upload it again.")
                continue
            result = self.evidence._process_file(ref, now)
            if result.status == "rejected":
                raise ValueError("Unsafe evidence cannot be attached to a ticket")
            records.append({"file_id": ref, "status": result.status, "delete_after": result.delete_after})
            if result.status != "processed":
                questions.append("An evidence file is unreadable, missing or expired. Please upload a clearer file.")
            else:
                values["order"].extend(v for v in result.evidence.order_ids if not v.upper().startswith("INV"))
                values["product"].extend(result.evidence.products)
        return records, list(dict.fromkeys(questions)), {k: list(dict.fromkeys(v)) for k, v in values.items()}

    def _priority(self, ticket, now, ratio):
        wait = BusinessCalendar(self.config.tickets).minutes_between(timestamp(ticket["started_at"]), now)
        parts = {"severity": {"low": 5, "medium": 20, "high": 40, "critical": 65}[ticket["severity"]],
                 "sentiment": {"positive": 0, "neutral": 0, "negative": 8, "angry": 15}[ticket["sentiment"]],
                 "waiting_time": min(15, int(wait // 120)),
                 "customer_impact": 20 if ticket["customer_impact"] >= 50 else 10 if ticket["customer_impact"] >= 5 else 0,
                 "sla": 25 if ratio >= 1 else 10 if ratio >= .75 else 0}
        score = sum(parts.values())
        priority = "P1" if score >= 65 else "P2" if score >= 40 else "P3" if score >= 20 else "P4"
        if ticket.get("escalated_at"):
            priority = "P1"
        ticket.update(priority=priority, priority_score=score, priority_factors=parts, waiting_business_minutes=round(wait, 2))

    def _missing(self, ticket):
        required = set(self.config.tickets.mandatory_fields) | set(self.config.tickets.required_by_category.get(ticket["category"], []))
        missing = [key for key in sorted(required) if not ticket.get(key)]
        if "evidence" in required and ticket["evidence"] and not any(e["status"] == "processed" for e in ticket["evidence"]):
            missing.append("evidence")
        contact = ticket.get("contact", "")
        if contact and not (self.masker.EMAIL.fullmatch(contact) or re.fullmatch(r"\+?[\d ()-]{7,22}", contact)):
            missing.append("valid_contact")
        return missing

    def _refresh(self, db, tickets, now, revision):
        calendar = BusinessCalendar(self.config.tickets)
        for ticket in tickets:
            if ticket["status"] == "resolved":
                continue
            for record in ticket["evidence"]:
                if record["delete_after"] and timestamp(record["delete_after"]) <= now:
                    record["status"] = "expired"
            ticket["missing_fields"] = self._missing(ticket)
            ticket["clarification_requests"] = ["Please provide " + key.replace("_", " ") + "." for key in ticket["missing_fields"]] + ticket["evidence_questions"]
            elapsed = calendar.minutes_between(timestamp(ticket["started_at"]), now)
            allowed = float(self.config.tickets.sla_minutes[ticket["sla_priority"]])
            ratio = elapsed / allowed
            ticket["sla"] = {"state": "breached" if ratio >= 1 else "warning" if ratio >= .75 else "on_track",
                             "allowed_minutes": allowed, "consumed_minutes": round(elapsed, 4), "consumed_fraction": round(ratio, 6),
                             "warning_at": calendar.add_minutes(timestamp(ticket["started_at"]), allowed * .75).isoformat(),
                             "due_at": calendar.add_minutes(timestamp(ticket["started_at"]), allowed).isoformat(), "policy_revision": revision}
            if ratio >= .75:
                self._event(db, ticket, "sla_warning", now, {"message": "75% of the SLA has been consumed", "policy_revision": revision}, once=True)
            if ratio >= 1:
                ticket["escalated_at"] = ticket["escalated_at"] or now.isoformat()
                self._event(db, ticket, "sla_breached", now, {"message": "SLA breached; escalated to support leads", "policy_revision": revision}, once=True)
            self._priority(ticket, now, ratio)
        self._route(db, tickets, now)
        for ticket in tickets:
            ticket["handoff_summary"] = self._handoff(ticket)
            self._save(db, ticket)

    def _route(self, db, tickets, now):
        config = self.config.tickets
        teams = {t["id"]: t for t in config.teams}
        loads = {a["id"]: 0 for a in config.agents}
        for ticket in sorted(tickets, key=lambda t: (t["priority"], t["started_at"], t["id"])):
            if ticket["status"] == "resolved":
                continue
            old = (ticket["team_id"], ticket["agent_id"], ticket["routing_reason"])
            ticket["team_id"], ticket["agent_id"] = None, None
            if ticket["escalated_at"]:
                eligible = [teams[config.escalation_team]]
            else:
                eligible = [t for t in config.teams if t["id"] != config.escalation_team and ticket["category"] in t["skills"]]
            reason, agent = "no_skilled_team", None
            if eligible:
                ticket["team_id"] = eligible[0]["id"]
                open_teams = [t for t in eligible if BusinessCalendar(config, t).is_open(now)]
                available = [t for t in open_teams if t["available"]]
                reason = "after_hours" if not open_teams else "team_unavailable" if not available else "no_available_agent"
                candidates = [a for a in config.agents if a["team"] in {t["id"] for t in available} and
                              a["available"] and ticket["category"] in a["skills"] and loads[a["id"]] < a["capacity"]]
                if candidates:
                    agent = min(candidates, key=lambda a: (loads[a["id"]] / a["capacity"], a["id"] != old[1], a["id"]))
            # Breaches must reach a supervisor even while waiting for customer details.
            if ticket["clarification_requests"] and not ticket["escalated_at"]:
                ticket["status"], reason, agent = "needs_information", "missing_information", None
            elif agent:
                ticket["agent_id"], ticket["team_id"] = agent["id"], agent["team"]
                loads[agent["id"]] += 1
                ticket["status"], reason = "escalated" if ticket["escalated_at"] else "assigned", "assigned"
            else:
                ticket["status"] = "escalated" if ticket["escalated_at"] else "queued"
            ticket["routing_reason"] = reason
            if old != (ticket["team_id"], ticket["agent_id"], reason):
                self._event(db, ticket, "routing_changed", now, {"team_id": ticket["team_id"], "agent_id": ticket["agent_id"], "reason": reason})

    def _handoff(self, ticket):
        evidence = ", ".join(e["file_id"] + " (" + e["status"] + ")" for e in ticket["evidence"]) or "not provided"
        summary = (f"{ticket['id']} | {ticket['priority']} | {ticket['category']}\n"
                   f"Customer: {'[CUSTOMER]' if ticket['customer'] else 'not provided'}; Contact: {'[CONTACT]' if ticket['contact'] else 'not provided'}\n"
                   f"Order: {ticket['order'] or 'not provided'}; Product: {ticket['product'] or 'not provided'}\n"
                   f"Issue: {ticket['issue'] or 'not provided'}\nEvidence: {evidence}\n"
                   f"SLA: {ticket['sla'].get('state', 'pending')}; Due: {ticket['sla'].get('due_at', 'pending')}\n"
                   f"Route: {ticket['team_id'] or 'unassigned'} / {ticket['agent_id'] or ticket['routing_reason']}\n"
                   f"Missing: {', '.join(ticket['missing_fields']) or 'none'}; Related group: {ticket['group_id']}")
        return self._mask(summary, ticket["customer"])

    def tick(self, now=None):
        """Internal worker entry point. HTTP callers must authorize ticket_admin first."""
        with self._transaction() as db:
            revision = self._policy(db)
            current = self._now(db, now)
            self._refresh(db, self._all(db), current, revision)
            return self._snapshot(db, current)

    def configure(self, changes, token=None, now=None):
        self.access.authorize(token, "ticket_admin")
        if not isinstance(changes, Mapping) or set(changes) - set(asdict(TicketConfig())):
            raise ValueError("Unknown ticket configuration field")
        with self._transaction() as db:
            revision = self._policy(db)
            candidate = TicketConfig(**{**asdict(self.config.tickets), **changes})
            validate_config(candidate)
            current = self._now(db, now)
            self.config.tickets = candidate
            self._set_meta(db, "policy", {"revision": revision + 1, "config": asdict(candidate)})
            self._refresh(db, self._all(db), current, revision + 1)
            return self._snapshot(db, current)

    def update(self, ticket_id, changes, token=None, now=None):
        self.access.authorize(token, "ticket_write")
        allowed = {"customer", "contact", "order", "product", "issue", "file_refs", "status"}
        if not isinstance(changes, Mapping) or set(changes) - allowed:
            raise ValueError("Unknown ticket update field")
        with self._transaction() as db:
            revision = self._policy(db)
            current = self._now(db, now)
            tickets = self._all(db)
            ticket = next((t for t in tickets if t["id"] == ticket_id), None)
            if ticket is None:
                raise ValueError("Ticket not found")
            if ticket["status"] == "resolved":
                raise ValueError("Resolved tickets are immutable; submit a new conversation")
            if self.injection.scan(json.dumps(changes)).score >= self.config.security.prompt_injection_threshold:
                raise ValueError("Ticket update contains unsafe instructions")
            for key in allowed - {"file_refs", "status"}:
                if key in changes:
                    ticket[key] = str(changes[key]).strip()
            ticket["issue"] = self._mask(ticket["issue"], ticket["customer"])
            if "issue" in changes:
                ticket["category"], ticket["intent"] = classify(ticket["issue"])
            if "contact" in changes and ticket["contact"]:
                ticket["identity"] = digest(ticket["contact"].casefold().strip())
            ticket["fingerprint"] = self._fingerprint(ticket["identity"], ticket["category"], ticket["intent"], ticket, ticket["issue"])
            if "file_refs" in changes or {"order", "product"} & set(changes):
                refs = changes.get("file_refs", [e["file_id"] for e in ticket["evidence"]])
                ticket["evidence"], ticket["evidence_questions"], values = self._evidence(refs, current)
                for key, options in values.items():
                    if not ticket[key] and len(options) == 1:
                        ticket[key] = options[0]
                    elif options and (len(options) != 1 or normalized(ticket[key]) != normalized(options[0])):
                        ticket["evidence_questions"].append(f"Confirm {key}: attached evidence has different values.")
            if "status" in changes:
                if changes["status"] != "resolved":
                    raise ValueError("The only manual status transition is resolved")
                self._refresh(db, tickets, current, revision)
                ticket["status"], ticket["resolved_at"] = "resolved", current.isoformat()
                ticket["agent_id"] = None
                self._event(db, ticket, "ticket_resolved", current)
            else:
                self._event(db, ticket, "ticket_updated", current)
            self._refresh(db, tickets, current, revision)
            return self._public(ticket)

    def use_live_clock(self, token=None):
        self.access.authorize(token, "ticket_admin")
        with self._transaction() as db:
            current = timestamp()
            clock = self._meta(db, "clock")
            if clock and current < timestamp(clock["now"]):
                raise ValueError("Live time is behind the simulated clock; keep this test workspace in simulation")
            db.execute("DELETE FROM metadata WHERE key='clock'")
        return self.tick()
