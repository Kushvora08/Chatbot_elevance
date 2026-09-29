# Knowledge-Base Update Pipeline

This workspace contains a production-shaped local pipeline for updating a chatbot knowledge base.

**GitHub Pages preview:** open the root [index.html](index.html) directly, or
publish it using [GITHUB_PAGES.md](GITHUB_PAGES.md). It is a self-contained browser
demo with sample-policy chat, citations, text-file review and downloadable ticket
drafts. It does not run the Python backend or send requests to real support agents.

**Publishing to GitHub and Render:** see [DEPLOYMENT.md](DEPLOYMENT.md).
The authenticated hosting entry point is `python -m kb_pipeline.hosted`; the
existing development console must remain local. Hosting includes protected
accounts, persistent state, a maintenance worker, and a Render blueprint.
Review the pilot limitations and resource costs in that guide before deployment.

It provides:

- Incremental ingestion of only new or modified documents.
- Duplicate detection by normalized document fingerprint.
- Quarantine for invalid, duplicate, oversized, binary, unsupported, or prompt-injection documents.
- Versioned knowledge-base snapshots with rollback.
- Quality gates that reject candidates when accuracy or grounding drops against the active version.
- Maintenance-window activation with pending approved versions.
- Automatic rollback when activation health checks fail within the five-minute grace period.
- Scheduler support for configurable update time and retries after 15, 30, and 60 minutes.
- Access control, sensitive-data masking, prompt-injection detection, and metrics for latency, failures, confidence, and escalations.
- Evidence analysis for customer messages, screenshots, invoices, PDFs, and product images.
- OCR/text extraction of order IDs, dates, amounts, products, and error codes.
- Clarification requests for conflicting evidence, low-quality images, missing uploads, and expired files.
- Background queue handoff for work that exceeds the configured processing timeout.
- Upload retention cleanup so files are deleted after the configured retention period.

## Quick Start

On Windows, double-click `start-chatbot.cmd` in this folder. It finds the installed
Python, starts the chatbot, and opens your browser. Keep the launcher window open
while using the app. An existing chatbot is reused, and an occupied port is skipped.
Python 3.10 or newer is required; this local preview needs no extra packages.

From a terminal, `python start_chatbot.py` does the same thing. Add `--no-browser`
to start without opening a browser, or `--port 8780` to choose another starting port.

For command-line knowledge-base operations:

Create a sample config:

```powershell
python -m kb_pipeline.cli --config kb_pipeline.config.json init-config
```

Run an update:

```powershell
python -m kb_pipeline.cli --config kb_pipeline.config.json update
```

Query the active version:

```powershell
python -m kb_pipeline.cli --config kb_pipeline.config.json --token service-token query "What does the policy say?"
```

## Chat and Operations Console

Run the local test console:

```powershell
python -m kb_pipeline.server --config kb_pipeline.config.json --port 8765
```

Open `http://127.0.0.1:8765` for the Aster customer-facing chatbot. The existing operations and testing console remains at `http://127.0.0.1:8765/console.html`.

The chat screen connects to the existing session, retrieval, evidence, and ticket APIs. It includes separate conversation tabs, native language selection, editable product/region context, source previews with a fresh access check, attachment review, recoverable error states, and a support-request form. Its responsive navigation and context panel become drawers on smaller screens. All visual assets and icons are served locally.

This remains a local development preview, not a public customer portal. The default service credential is a demo key and is configurable in Preferences; deployment needs server-side customer authentication, scoped authorization, HTTPS, and a credential-free browser client. Chat resume credentials are tab-scoped. Transcripts remain in the existing session store, while attachment review displays and rejected-file messages last only for the current page. Evidence review is separate from the policy answer; a conflicting, unsafe, low-quality, or queued attachment withholds a policy reply and displays the analyzer's clarification. Real OCR and background delivery retain the existing backend's integration limitations. The redesign does not replace rules-based language understanding with a generative model.

The console lets you seed sample documents, edit source files, run updates, simulate maintenance-window times, force health failures or outages, activate pending versions, roll back, query the active knowledge base, analyze uploaded evidence, and inspect quarantine/metrics/version state.

## Evidence Analysis

The Evidence tab supports local uploads and sample scenarios for matching invoices, mismatched invoices, blurred images, hidden prompt-injection text, sensitive data, and product images.

For image OCR in this dependency-free local console, upload a file and optionally provide an OCR text override. The production-facing analyzer also supports `.ocr.txt` sidecar files and extracts text from readable PDFs when available. If no reliable text can be extracted, the result requests a clearer file instead of guessing.

The default development tokens are `admin-token`, `service-token`, and `viewer-token`. Replace them in production config, or use `sha256:<digest>` token keys if you do not want plaintext tokens in the file.

## Evaluation Cases

Quality cases are JSON objects:

```json
[
  {
    "question": "What is the refund window?",
    "required_terms": ["30 days", "receipt"],
    "forbidden_terms": ["90 days"],
    "source_hint": "refund"
  }
]
```

A candidate is rejected if it falls below configured accuracy or grounding thresholds, or if either metric regresses compared with the active version.

## Support Tickets

Open the **Tickets** tab to create tickets from unresolved customer conversations.
The Query tab also offers **Create support ticket** when an answer needs escalation.

1. Select **New conversation**, choose a sample, and select **Load sample**.
2. Select **Create tickets**. Submit the same sample again to verify duplicate merging.
3. Try **Missing details**, then fill the requested details in the selected ticket.
4. Try **Unrelated issues** to get separate tickets; **Related billing issues** produces separate tickets in one group.
5. Expand **SLA and Routing** to edit deadlines, working days, holidays, agent capacity and availability. Changes apply immediately to open tickets.
6. Set **Test time (UTC)** and select **Apply time** before creating a ticket. With a 60-minute SLA, advancing by 45 minutes produces a warning; advancing by 60 minutes produces an escalation.

The simulated clock persists across refreshes and restarts and never advances by itself.
**Advance 1 hour** moves it forward; **Use live time** resumes real monitoring when the real clock is not behind the simulation.
The server evaluates SLAs and routing every second even without browser activity. While the server is stopped, tickets remain on disk; elapsed time is accounted for at restart.

### Rules and Defaults

- Customer, contact and issue are mandatory. Shipping/billing also require an order; technical/product issues require a product. Evidence can be made mandatory. Missing or ambiguous values produce clarification requests instead of invented details.
- Intake accepts customer-only transcripts or explicit `issues` objects. Rules recognize common delivery, billing, login, outage and product issues. For complex conversations, supply explicit issues with their own order/product/category to control splitting precisely.
- Duplicate matching uses customer identity, order/product, issue intent and error code. It merges open tickets within 168 hours without resetting their SLA. Anonymous requests require a stable `conversation_id` to match. Related issues share a group, while distinct issues retain separate tickets. Resolved tickets are immutable and subsequent requests create new tickets.
- Priority scores combine severity (5/20/40/65), sentiment (0/8/15), business-time waiting (up to 15), customer impact (0/10/20), and SLA consumption (0/10/25). Scores of 65/40/20 select P1/P2/P3; lower scores select P4. Breached tickets are P1.
- Default resolution SLAs are P1=60, P2=240, P3=480 and P4=960 business minutes. The contractual priority is fixed at intake so rising urgency cannot move the deadline accidentally. Runtime policy edits recalculate the SLA from the original conversation start under the current calendar, including retroactive holiday changes. Previously escalated tickets stay escalated until resolved.
- Business hours default to 09:00-17:00 UTC, excluding Saturday/Sunday and configured ISO-date holidays. Closing time is exclusive. Fixed timezone offsets such as `+05:30` work without dependencies; IANA zones use `zoneinfo` and require system timezone data or the `tzdata` package. Team-specific `timezone`, `business_start` and `business_end` overrides affect routing.
- Routing requires both team and agent skills, availability and spare capacity. Eligible agents are compared by workload/capacity; existing assignments win ties. Higher-priority tickets are considered first. After-hours or unavailable teams keep tickets queued. Breaches produce a durable supervisor escalation even when supervisors are unavailable; assignment retries automatically.
- Warnings at 75% and escalations at 100% are recorded once per ticket in a durable activity/notification outbox. The console displays them. Email, SMS and external help-desk delivery are integration points, not configured services.
- Tickets, runtime policy, clock and events persist transactionally in `.kb_state/tickets.sqlite3`. Runtime ticket settings in this database take precedence over startup file settings after initialization. Back up this database with the rest of the state directory.
- The ticket store retains customer/contact fields for support operations. API ticket views, handoffs and activity are masked; transcript text is not stored verbatim. Evidence uses managed upload IDs and is checked for injection, expiry and contradictions. The existing local OCR adapter limitations still apply.

### Ticket API

All API calls require a configured token, using `Authorization: Bearer <token>`, `X-KB-Token`, or a POST `token` field. There is no implicit admin fallback. Replace the demonstration tokens before exposing the server beyond localhost. Viewers can read masked tickets; service roles can create/update tickets; only admins can change policy or advance the clock through its endpoint.

| Endpoint | Purpose |
| --- | --- |
| `GET /api/tickets` | Masked tickets, events, clock, policy and counts |
| `POST /api/tickets/from-conversation` | Convert `{conversation, now?}`; a resolved conversation creates nothing |
| `POST /api/tickets/update` | `{ticket_id, changes, now?}`; complete details, replace `file_refs`, or set `status: resolved` |
| `POST /api/tickets/config` | `{config, now?}`; validate, persist and apply partial runtime policy changes |
| `POST /api/tickets/tick` | `{now?}`; evaluate the queue and optionally advance simulated time |
| `POST /api/tickets/live-clock` | Resume real-time monitoring |

`POST /api/config` also accepts a `tickets` section. `POST /api/query` can include a `conversation` object: if the answer is escalated, the response includes `ticket_workflow` with tickets and clarification requests.

Example conversation:

```json
{
  "conversation_id": "conversation-123",
  "customer": "Jordan Lee",
  "contact": "jordan@example.com",
  "order": "ORD-1001",
  "product": "Blue Widget",
  "message": "My order has not arrived. I am frustrated.",
  "unresolved": true,
  "customer_impact": 1,
  "file_refs": []
}
```

The Python entry points are `pipeline.tickets.convert(...)`, `update(...)`, `configure(...)`, `snapshot(...)`, and the internal scheduler method `tick(now=...)`. `now` accepts an aware datetime or ISO timestamp; naive timestamps mean UTC. The local HTTP test surface supports simulated times; keep simulation endpoints restricted to trusted operators.

Run the regression suite with `python -m unittest discover -s tests -v`. Ticket coverage includes concurrent duplicates, related/unrelated issues, mandatory-field completion, business calendars, exact warning/breach boundaries, unavailable teams, capacity, live monitoring, outages, restarts, runtime policy changes, access control, masking and unsafe evidence.

## Metadata-Aware Knowledge Assistant

The **Query** tab now provides product, region and policy-date controls, cited answers, and an authenticated source viewer. Leave the policy date blank for current questions, or choose a historical date. Dates written in questions must use YYYY-MM-DD; ambiguous or conflicting dates trigger clarification.

To try the built-in evaluation:

1. Use `admin-token`, open **Query > Evaluation Controls**, and select **Stage examples**.
2. Set **Test clock (UTC)** inside your maintenance window, for example `2026-09-07T02:10` with the default configuration. Select **Evaluate update**. This runs RAG quality cases and uses the existing activation and health gates. Outside maintenance, approved updates remain pending. A failed health gate still rolls back.
3. Select a scenario, select **Load question**, then **Ask**. Current US refunds return 30 days; the historical scenario returns 14 days; EU refunds return 45 days. All factual output has `[S1]`-style references and matching source records.
4. Switch to `viewer-token` for public-only retrieval. The restricted credit scenario refuses; `admin-token` can retrieve it. Switching tokens clears previously displayed evidence.
5. Try conflicting policies, expired rebates, the troubleshooting guide, and the unsupported draft. The malicious example is quarantined during ingestion.

### Document Format

Create/edit metadata under **Documents > Editor > Document Metadata**, or ingest a UTF-8 JSON envelope:

```json
{
  "metadata": {
    "title": "Atlas Hub Refund Policy",
    "document_type": "policy",
    "policy_id": "atlas-refunds",
    "topic": "refunds",
    "document_version": "2.1",
    "product": "Atlas Hub",
    "region": "US",
    "access_level": "public",
    "effective_date": "2026-01-01",
    "expiry_date": "2027-01-01"
  },
  "content": "Refunds are available for 30 days with a receipt."
}
```

The six version/scope/access/date fields are required for structured documents. `expiry_date: null` means no scheduled expiry. Supported document types are `product`, `faq`, `policy`, `troubleshooting`, and `guide`. Use the same stable `policy_id` for revisions and regional variants of one policy; give related FAQs/guides the same `topic` when a policy should take precedence. Numeric versions compare numerically, so `2.10` is newer than `2.2`.

Markdown can alternatively start with `---json`, a JSON metadata object, and a closing `---` on its own line. YAML headers and malformed/partial metadata are rejected rather than treated as unrestricted content. Existing plain documents receive version `1`, wildcard product/region, no known dates, and **internal** access by default. Undated legacy documents are never used for historical answers. Add explicit metadata to production policies.

### Selection and Grounding

- Access comes from the authenticated token, never a role or access level in the question/request. Default grants are viewer=public, service=public/internal, admin=all three levels. Configure grants in `rag.access_by_role`. Authorisation runs before relevance ranking, scope inference and conflict detection.
- Effective dates are inclusive; expiry dates are exclusive, in UTC. Future records do not apply to current questions. Historical retrieval uses the specified date; requests for future or ambiguous dates ask for clarification.
- Within a policy/product/region family, the newest effective revision wins, with numeric version breaking same-date ties. An expired successor does not reactivate its superseded predecessor. Exact scope overrides a wildcard when both product and region applicability can be established. Equal precedence with different text refuses to choose arbitrarily.
- A policy overrides related FAQ/guide records sharing its topic. Additional checks detect conflicting numbers and negations in similar source statements before top-k limiting. Unresolved conflicts and incomplete evidence produce no factual answer or source excerpts.
- Approved prior snapshots retain overwritten policy revisions for historical questions while their logical policy still exists at the same path. Both historical and current access levels are enforced. Deleted documents and rolled-back/rejected snapshots are not resurrected. History is bounded by the configured snapshot history retention; unavailable history results in refusal.
- Every factual answer is assembled from complete source statements and checked against its citations. Citation records include the exact quote, document hash/version, product, region, access, effective/expiry dates, and active KB snapshot ID. Source viewing rechecks authorisation, date and hash. After the active snapshot changes, ask again to obtain current citation links.
- Prompt-injection checks scan raw and decoded document content, including HTML entities, Unicode formatting characters, metadata and indexed content. Retrieved content is treated as evidence, never executable instructions. Metrics store answer status, citation count and policy date without copying question text or restricted excerpts.

This local implementation uses lexical retrieval and extractive answers; it does not call an external language model or embedding service. Conflict/injection detection is conservative and rule-based, not a general semantic proof. A supplied draft must match complete cited source statements exactly, preserving conditions and negations; even a plausible paraphrase is rejected when it cannot be verified. `confidence` reports verified evidence coverage, not a calibrated probability of factual truth.

### Query API

`POST /api/query` accepts `question`, optional `product`, `region`, `as_of`, `top_k`, and `proposed_answer`. An optional `now` simulates the current clock and requires an admin scheduling permission over HTTP. Request fields named `role` or `access_level` never grant access. Example:

```json
{
  "question": "What was the refund policy?",
  "product": "Atlas Hub",
  "region": "US",
  "as_of": "2025-06-01"
}
```

Responses include `status`, `answer`, `citations`, `contexts`, `as_of`, `clarification_request`, and `unsupported_claims`. Statuses are `answered`, `insufficient_evidence`, `needs_clarification`, `refused`, and `unsupported_claim`. For draft validation, use the citation IDs from a query response, such as `Refunds are available for 30 days with a receipt. [S1]`.

`GET /api/rag/catalog` returns only authorised product/region options. `GET /api/rag/source` follows the URL in a citation using the same authentication header. Raw source editing and operational document inventories require the update permission, preventing retrieval restrictions from being bypassed through the console.

Quality cases now accept `product`, `region`, `as_of`, `role`, and `expected_status` alongside required/forbidden answer terms. Both candidate and baseline quality scores use the same authorised, date-aware, cited-answer path as live queries.

## Conversation Sentiment and Escalation

Open **Conversations** at `http://127.0.0.1:8765/console.html`. Select a scenario, **Load sample**, then **Send message**. Send another message in the same conversation to test history. **New conversation** starts an independent case; **Mark resolved** stops its unresolved timer and closes its escalation records.

Quick checks:

1. Send the normal complaint, then send `Great.` in the same conversation. Expect contextual sarcasm, an empathetic acknowledgement, and a `repeated_negative` escalation.
2. Send the calm high-risk sample in a new conversation. Expect `duplicate_payment` even without angry wording.
3. In **Test Clock**, choose a future Friday after the configured closing time. Send an urgent complaint: expect **on call**. A normal complaint goes to **next working day**, skipping configured weekends and holidays.
4. Send one normal complaint, then advance 16 minutes. Expect `negative_timeout`. Exactly 15 minutes does not trigger the "more than 15" rule. Resolve another case before advancing and verify it does not escalate.
5. Try Spanish/Hindi, unsafe instructions, and `viewer-token` or `bad-token`. Read-only viewers cannot send, resolve, or change time; unknown tokens cannot read the queue.

The conversation and ticket screens share one persisted clock and the **Tickets > Policy** business calendar. Calendar changes apply at the next monitor tick. Simulated time is frozen until explicitly advanced; it cannot go backwards. Returning to live time is blocked while simulation is ahead of real time. Use temporary state directories for automated tests so test clocks do not alter an operational queue.

### Rules and Guarantees

- English, Spanish, French, German, Portuguese, Hindi/Hinglish and Arabic phrase rules support mixed-language messages. Six independent label scores are returned alongside a primary label, language, ambiguity flag and method. Scores are **heuristic strengths, not calibrated probabilities**. Unsupported text has low confidence and asks for clarification. This is an offline baseline, not a trained multilingual classifier; subtle sarcasm, dialects, obfuscation and personal-data recognition need broader models and held-out evaluation before production use.
- Only customer turns count toward sentiment; imported agent turns do not trigger risk. Sarcasm considers positive/negative contradiction and recent customer context. Two negative/frustrated/sarcastic messages in an unresolved conversation trigger escalation. A positive or neutral follow-up does not silently resolve the case or reset its first-negative timer.
- Account compromise, duplicate payment and legal threats have a separate risk detector, independent of mood/confidence. High-risk cases escalate immediately. Explicit negation is suppressed; recognised hypothetical questions request clarification. Urgent after-hours complaints and all high-risk after-hours cases enter the persistent on-call queue. Normal complaints retain next-working-day scheduling even if the 15-minute or repeated-negative rule escalates them for supervisor review.
- The 15-minute rule uses elapsed wall time, including nights and weekends, not SLA business time. The server evaluates conversations every second without requiring another message. Worker failures are recorded and shown on the screen; the next tick retries. Durable SQLite state catches overdue cases after restart.
- Each escalation is unique per conversation and condition and includes the reason, activated condition, first-trigger time, masked summary, negative count, elapsed time, calendar revision and queue. Queue records remain pending until the case is resolved. **No external pager, email or SMS delivery is connected.** A production notification worker must consume this outbox with delivery acknowledgements/retries.
- Message IDs provide atomic retry deduplication across processes. Reusing an ID with different content is rejected; genuinely repeated complaints with new message IDs count separately. Raw messages are analysed in memory; persisted history and summaries are masked. Logs contain rule labels, confidence, latency, queue and error types, not customer text. Provide `customer` when known to mask that name in the submitted transcript. Supply opaque IDs, not email addresses or other personal identifiers.
- The response acknowledgement is a separate field from the knowledge answer. Tone never rewrites verified policy text, citations, access checks or refund terms. Retrieval failures return an unavailable answer without losing the already-persisted safety escalation. The latest 100 masked turns are kept per conversation; cumulative risk/negative counts and the first-negative timestamp are retained.

### Conversation API

All routes require the normal bearer token. Reads use `ticket_read`; messages/resolution use `ticket_write`; explicit clock changes and imported timestamps require `ticket_admin`. These are staff/service permissions, not per-customer identity isolation.

| Endpoint | Purpose |
| --- | --- |
| `GET /api/conversations` | Masked history, sentiment, escalation outbox, counts, shared clock and calendar |
| `POST /api/conversations/message` | `{conversation, now?, product?, region?, as_of?}`; monitor and return an unchanged RAG answer plus tone |
| `POST /api/conversations/resolve` | `{conversation_id}`; stop monitoring the resolved case |
| `POST /api/conversations/tick` | `{now?}`; process due rules and optionally advance the shared clock |
| `POST /api/conversations/live-clock` | Resume the shared live clock |

```json
{
  "conversation": {
    "conversation_id": "C-support-123",
    "message_id": "M-001",
    "message": "Hello, I noticed I was charged twice. Could you check?",
    "language": "auto"
  }
}
```

Optional `history` is a list of `{role, content, at?}` entries and can only be imported at conversation creation. Timestamps must be chronological and no later than the shared clock; absent timestamps mean intake time, not an invented historical time. Never infer resolution from sentiment alone: use the resolve endpoint. `POST /api/query` also accepts the same `conversation` fields and returns `conversation_monitor` and `tone` alongside its existing response and ticket handoff.

Run `python -m unittest discover -s tests -v` for the complete regression suite. The new cases cover multilingual labels, historical sarcasm, calm risk, masking, concurrent retries, outages, persistence, exact timeout boundaries, weekends/holidays, runtime calendars, time zones, worker-driven escalation and access control.

## Multilingual Customer Sessions

**Languages & Sessions** adds Tamil (`ta`), Telugu (`te`) and Bengali (`bn`) to the existing seven languages. It includes native-script requests, common Latin-script transliterations, mixed-language detection, language switching, spelling-tolerant support intents, separate handling of multiple requests, and explicit corrections. Low language or intent confidence produces a clarification, not a guessed policy answer. Recognized explicit language preferences persist; otherwise replies follow the detected language. Original entity values are never spell-corrected, translated, case-folded or date-reformatted.

### Try It

1. Select **New customer**, choose **Tamil request**, **Load message**, then **Send**. Open **Retained Context** to inspect the exact name and order ID.
2. Switch the sample to Telugu or Bengali in the same customer tab. For policy examples, set product scope to `Atlas Hub` and region scope to `US`.
3. Try transliterated Tamil, mixed-language input, a spelling error, multiple requests, and the corrected-order sample. `And that one?` after multiple requests asks for clarification. Selecting a single order after an ambiguous multi-order request keeps the original intent.
4. Create a second customer with different identifiers. Switching customer tabs preserves each customer's separate history, context and draft.
5. Under **Test Clock**, advance exactly 30 minutes: the session expires even without a new message. **Return to chat** starts a new session and restores the previous summary if the last customer message was no more than 24 hours ago. After 24 hours and one second, no old context is restored. Polling, retries and viewing history do not reset inactivity.
6. **Language & Session Settings** controls enabled languages, default language, both confidence thresholds, retained messages (minimum 10), inactivity minutes and summary recovery hours. Updates persist across restarts and apply to existing sessions on the next monitor tick. Time controls are shared with the ticket/conversation screens and remain admin-only.

Use a past simulated starting time or a disposable state directory for expiry tests. A clock moved into the future cannot resume real time until real time catches up. The browser console retains test-customer resume keys in tab-scoped session storage so reloads can recover those customers. Changing the staff token clears these keys and the displayed private context; closing the browser tab discards them. The API can restore sessions across client restarts when the caller securely retains its resume key.

### Session Contract

- A session ends at `last_activity_at + inactivity_minutes`, inclusive; the default is 30 minutes. The background worker checks every second. New messages enforce expiry synchronously too, so a stopped or delayed worker cannot allow an expired session to remain active.
- Restoration is allowed through `last_customer_message_at + restoration_hours`, inclusive; default 24 hours. The next instant starts a clean session. Restoration creates a new session ID, copies the structured summary and bounded history, and marks `context_restored`. Repeated resumes without customer messages cannot extend the recovery window. Expired old context is logically removed after its retention window; this is not a forensic secure erase of SQLite pages or backups.
- By default 20 customer turns and their replies are retained, with a configurable minimum of 10. Structured names, order IDs, dates, product codes, outstanding intents and explicit correction history survive transcript trimming. A correction updates current facts but retains an audit of the prior values. Multiple unresolved referents require clarification.
- Session context is stored separately in `sessions.sqlite3`. Each customer has a server-generated opaque resume key stored only as a hash on the server, bound to the initiating API credential. Session IDs and caller-supplied customer labels do not grant access. Correct credentials are required for both reads and writes, including simultaneous customers using the same staff/service token. Message IDs are idempotent within a customer, not globally; different customers may use the same message ID.
- The customer-private context preserves names exactly. Email, phone and payment patterns are masked in retained message text; known names are also masked before forwarding to the shared support log. Session metrics contain confidence, language, request count and timings, not customer content, resume keys or names. The private SQLite database is not encrypted by this local implementation; protect its filesystem, backups and hosting account before production use.
- Session expiry is not issue resolution. Existing support escalations and tickets stay open, and high-risk detection still runs when intent is unclear or the knowledge service is unavailable.

### API

| Endpoint | Purpose |
| --- | --- |
| `POST /api/sessions/start` | Create a customer key/session, or pass `resume_key` to resume the current customer |
| `POST /api/sessions/view` | `{resume_key, session_id}`; private context without extending activity |
| `POST /api/sessions/message` | `{resume_key, session_id, message_id, message, language?, entities?, product?, region?, as_of?, now?}` |
| `GET /api/sessions/config` | Current language/session policy, revision and shared clock |
| `POST /api/sessions/config` | Admin-only `{config}` partial policy update |
| `POST /api/sessions/tick` | Admin-only `{now?}` simulated clock and expiry evaluation |
| `POST /api/sessions/live-clock` | Admin-only return to the shared live clock |

Start returns a `resume_key` only for a new customer; keep it secret. Send credentials in JSON over authenticated POST, not in URLs. `ticket_write` is required for customer session operations, `ticket_read` for settings, and `ticket_admin` for configuration or explicit timestamps. These are local staff/service credentials; connect verified customer authentication, TLS and a secure cookie or credential vault before exposing the service publicly. Default demo tokens are not production authentication.

Optional `entities` contains arrays named `names`, `order_ids`, `dates` and `product_codes`. Use these for identifiers already extracted by a trusted upstream integration. Natural messages can also supply labeled fields, for example `My name is Zoë Nair; order: ORD-aBc-004; product: SKU-X9; date: 2026-09-17`. An expired current session automatically rotates when a fresh message arrives; use the returned new session ID for later messages. Stale session IDs cannot append to a newer session. Replays never reset the idle clock.

Responses contain `session`, `interpretation`, `reply`, per-request `answers`, `citations`, support routing and configuration revision. Interpretation includes separate language/intent confidence, detected languages, spelling interpretations and clarification reasons. Multiple policy answers have globally distinct citation IDs. Product/region/policy-date scope is retained until explicitly changed; pass an empty string to clear a scope field.

**Limitations:** language and intent recognition remain conservative offline rules, not trained NLU or a general translation model. Common support wording is covered; unseen dialects, arbitrary intent and complex corrections can require clarification. Confidence scores are heuristic, not calibrated accuracy estimates. Clarification and session replies are localized; verified policy excerpts remain verbatim in the source language to preserve grounding and identifiers. Full translated policy answers need vetted localized documents or a validated translation provider. No live order/payment actions are performed or inferred from customer statements.
