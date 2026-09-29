from __future__ import annotations

from typing import Any, Mapping
import hashlib
import json
import re
import unicodedata
import uuid

from .tickets import BusinessCalendar, TicketWorkflow, timestamp


LABELS = ("positive", "neutral", "negative", "frustrated", "urgent", "sarcastic")
NEGATIVE = {"negative", "frustrated", "sarcastic"}
# Compact, inspectable offline rules. Scores express rule strength, not calibrated probabilities.
LEXICONS = {
    "en": {
        "positive": "thank you|thanks|great|wonderful|perfect|excellent|happy|love it|brilliant|fantastic",
        "negative": "bad|terrible|awful|disappointed|unhappy|unacceptable|not working|does not work|broken|still waiting|not arrived|has not arrived|never arrived|late|delayed|no response|not helpful|failed|failure|useless|not happy|not satisfied|horrible",
        "frustrated": "frustrated|furious|angry|fed up|ridiculous|again|third time|how many times|still not|tired of|keep asking",
        "urgent": "urgent|urgently|asap|immediately|emergency|right now|cannot wait|critical|service is down|production down|outage",
        "sarcastic": "yeah right|as if|thanks for nothing|just what i needed|what a surprise|/s",
        "neutral": "hello|order|refund|please|policy|help|invoice|account|payment|delivery|when|where|how|what|can you",
    },
    "es": {
        "positive": "gracias|genial|excelente|perfecto|maravilloso|contento",
        "negative": "malo|terrible|decepcionado|inaceptable|no funciona|roto|sigo esperando|no ha llegado|no llego|retrasado|sin respuesta|no estoy contento",
        "frustrated": "frustrado|frustrada|furioso|harto|harta|enojado|otra vez|tercera vez",
        "urgent": "urgente|inmediatamente|emergencia|ahora mismo|no puedo esperar",
        "sarcastic": "si claro|gracias por nada|justo lo que necesitaba",
        "neutral": "hola|pedido|reembolso|factura|cuenta|por favor",
    },
    "fr": {
        "positive": "merci|super|excellent|parfait|formidable|content",
        "negative": "mauvais|decu|deçue|inacceptable|ne fonctionne pas|casse|toujours en attente|pas arrive|aucune reponse|en retard|pas satisfait",
        "frustrated": "frustre|frustree|furieux|marre|encore une fois|troisieme fois|en colere",
        "urgent": "urgent|urgente|immediatement|urgence|tout de suite",
        "sarcastic": "mais bien sur|merci pour rien|quelle surprise",
        "neutral": "bonjour|commande|remboursement|facture|compte|s'il vous plait",
    },
    "de": {
        "positive": "danke|toll|super|wunderbar|perfekt|ausgezeichnet|zufrieden",
        "negative": "schlecht|enttauscht|inakzeptabel|funktioniert nicht|kaputt|warte immer noch|nicht angekommen|keine antwort|verspatet|nicht zufrieden",
        "frustrated": "frustriert|wutend|sauer|schon wieder|zum dritten mal|habe genug",
        "urgent": "dringend|sofort|notfall|unverzuglich|jetzt sofort",
        "sarcastic": "ja klar|danke fur nichts|na toll",
        "neutral": "hallo|bestellung|ruckerstattung|rechnung|konto|bitte",
    },
    "pt": {
        "positive": "obrigado|obrigada|otimo|excelente|perfeito|maravilhoso|feliz",
        "negative": "ruim|decepcionado|inaceitavel|nao funciona|quebrado|ainda esperando|nao chegou|sem resposta|atrasado|nao estou satisfeito",
        "frustrated": "frustrado|frustrada|furioso|irritado|de novo|terceira vez|estou farto",
        "urgent": "urgente|imediatamente|emergencia|agora mesmo",
        "sarcastic": "sim claro|obrigado por nada|era so o que faltava",
        "neutral": "ola|pedido|reembolso|fatura|conta|por favor",
    },
    "hi": {
        "positive": "धन्यवाद|शुक्रिया|बहुत अच्छा|बढ़िया|खुश|shukriya|dhanyavaad|bahut accha",
        "negative": "खराब|निराश|नहीं आया|नहीं मिला|काम नहीं|जवाब नहीं|इंतजार|bekar|kharab|nahi aaya|nahi mila|kaam nahi|jawab nahi",
        "frustrated": "परेशान|गुस्सा|बार बार|फिर से|तंग|pareshan|gussa|baar baar|phir se|tang aa",
        "urgent": "तुरंत|अभी|आपातकाल|जरूरी|ज़रूरी|जल्दी|turant|abhi|jaldi|urgent",
        "sarcastic": "वाह|क्या बात है|wah|kya baat hai",
        "neutral": "नमस्ते|ऑर्डर|वापसी|कृपया|namaste|kripya|order",
    },
    "ta": {
        "positive": "நன்றி|அருமை|மகிழ்ச்சி|nandri|arumai",
        "negative": "மோசம்|வரவில்லை|வேலை செய்யவில்லை|பதில் இல்லை|தாமதம்|varala|varavillai|mosam|velai seyyala",
        "frustrated": "எரிச்சல்|கோபம்|விரக்தி|மீண்டும்|kovam|erichal|marubadiyum",
        "urgent": "அவசரம்|உடனே|உடனடி|avasaram|udane",
        "sarcastic": "இதுதான் எனக்கு தேவைப்பட்டது",
        "neutral": "வணக்கம்|ஆர்டர்|பணம்|திரும்ப|எப்போது|vanakkam|eppo",
    },
    "te": {
        "positive": "ధన్యవాదాలు|బాగుంది|సంతోషం|dhanyavadalu|bagundi",
        "negative": "రాలేదు|పనిచేయడం లేదు|చెత్త|ఆలస్యం|సమాధానం లేదు|raledu|panicheyatledu|chetta|alasyam",
        "frustrated": "కోపం|విసుగు|మళ్లీ|kopam|visugu|malli",
        "urgent": "అత్యవసరం|వెంటనే|తక్షణం|atyavasaram|ventane",
        "sarcastic": "నాకు కావలసింది ఇదే",
        "neutral": "నమస్కారం|ఆర్డర్|డబ్బు|ఎప్పుడు|namaskaram|eppudu",
    },
    "bn": {
        "positive": "ধন্যবাদ|দারুণ|খুশি|dhonnobad|dhanyabad|darun",
        "negative": "খারাপ|আসেনি|কাজ করছে না|উত্তর নেই|দেরি|kharap|aseni|kaj korche na|deri",
        "frustrated": "বিরক্ত|রাগ|হতাশ|আবার|birokto|raag|hotash|abar",
        "urgent": "জরুরি|এখনই|অবিলম্বে|joruri|ekhoni",
        "sarcastic": "এটাই তো দরকার ছিল",
        "neutral": "নমস্কার|অর্ডার|টাকা|কখন|nomoskar|kokhon",
    },
    "ar": {
        "positive": "شكرا|شكراً|رائع|ممتاز|سعيد",
        "negative": "سيء|سيئة|محبط|لا يعمل|لم يصل|لم تصل|لا رد|متأخر|غير راض",
        "frustrated": "غاضب|غاضبة|سئمت|مرة أخرى|مرة اخرى|مستاء",
        "urgent": "عاجل|فورا|فوراً|طوارئ|الآن|الان",
        "sarcastic": "شكرا على لا شيء|يا للسخرية",
        "neutral": "مرحبا|طلب|فاتورة|حساب|من فضلك",
    },
}

RISKS = {
    "account_compromise": "account was hacked|account is hacked|account hacked|account has been hacked|account compromised|account is compromised|account was compromised|account has been compromised|someone accessed my account|unauthorized access|unauthorised access|stolen credentials|cuenta hackeada|cuenta comprometida|acceso no autorizado|compte pirate|compte compromis|acces non autorise|konto gehackt|konto kompromittiert|unbefugter zugriff|conta invadida|conta comprometida|acesso nao autorizado|खाता हैक|अकाउंट हैक|खाते में अनधिकृत|account hack ho|حسابي مخترق|اختراق حساب|دخول غير مصرح",
    "duplicate_payment": "charged twice|billed twice|double charged|double-charged|duplicate payment|duplicate charge|charged me twice|paid twice|cobrado dos veces|cobraron dos veces|pago duplicado|doble cobro|debite deux fois|paye deux fois|paiement en double|paiement duplique|doppelt berechnet|doppelt abgebucht|doppelte zahlung|cobrado duas vezes|pagamento duplicado|दो बार भुगतान|दो बार पैसे|दो बार काट|दोहरा भुगतान|paise do baar|do baar payment|تم الخصم مرتين|دفعت مرتين|دفع مكرر",
    "legal_threat": "sue you|sue your|take legal action|taking legal action|will sue|lawsuit|contact my lawyer|contacting my lawyer|legal proceedings|legal action against|demandarlos|voy a demandar|acciones legales|mi abogado|porter plainte|poursuites judiciaires|mon avocat|rechtliche schritte|mein anwalt|verklagen|processar voces|acao judicial|meu advogado|कानूनी कार्रवाई|मुकदमा|वकील|kanooni karwai|vakil|اجراءات قانونية|إجراءات قانونية|ساقاضي|سأقاضي|محامي",
}
for _risk, _phrases in {
    "account_compromise": "கணக்கு ஹேக்|கணக்கு திருடப்பட்டது|account hack aachu|ఖాతా హ్యాక్|అకౌంట్ హ్యాక్|account hack ayyindi|অ্যাকাউন্ট হ্যাক|অ্যাকাউন্ট চুরি|account hack hoyeche",
    "duplicate_payment": "இருமுறை பணம்|இரண்டு முறை பணம்|rendu murai panam|రెండుసార్లు డబ్బు|రెండు సార్లు డబ్బు|rendu sarlu dabbu|rendusarlu dabbu|দুবার টাকা|দুইবার টাকা|duibar taka|dubar taka",
    "legal_threat": "சட்ட நடவடிக்கை|வழக்கு தொடர|satta nadavadikkai|చట్టపరమైన చర్య|దావా వేస్తా|আইনি ব্যবস্থা|মামলা করব|aini byabostha|mamla korbo",
}.items():
    RISKS[_risk] += "|" + _phrases

REASONS = {
    "account_compromise": "Possible account compromise",
    "duplicate_payment": "Possible duplicate payment",
    "legal_threat": "Legal threat or proceedings",
    "repeated_negative": "Repeated negative customer messages",
    "negative_timeout": "Negative conversation unresolved for more than 15 minutes",
    "urgent_after_hours": "Urgent complaint received outside business hours",
}


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).casefold().replace("’", "'")
    # Fold Latin accents without destroying vowel marks in Hindi or Arabic.
    text = "".join("".join(c for c in unicodedata.normalize("NFD", char) if not unicodedata.combining(c))
                   if "LATIN" in unicodedata.name(char, "") else char for char in text)
    return " ".join(c for c in text.split())


def matches(text: str, phrases: str):
    alternatives = []
    for phrase in phrases.split("|"):
        escaped = re.escape(normalize(phrase))
        alternatives.append(escaped if re.search(r"[\u0980-\u0cff]", phrase) else r"(?<!\w)" + escaped + r"(?!\w)")
    pattern = "|".join(alternatives)
    return list(re.finditer(pattern, text))


def negated(text: str, match) -> bool:
    prefix = re.split(r"[.!?;,]", text[:match.start()])[-1][-65:]
    return bool(re.search(r"\b(?:not|never|no|isn't|wasn't|haven't|hasn't|didn't|nicht|kein|keine|pas|nao)\s+(?:(?:been|being|actually|really|very|at all|de|du|est|ete)\s+){0,2}$", prefix))


class SentimentAnalyzer:
    def analyze(self, message: str, history: list[dict] | None = None, language: str | None = None) -> dict:
        text = normalize(message)
        language = language or "auto"
        if language != "auto" and language not in LEXICONS:
            raise ValueError("Unsupported sentiment language")
        history = [{**m, "analysis": m.get("analysis") or self.analyze(m.get("content", ""), language=language)}
                   for m in (history or []) if m.get("role") in {"customer", "user"}]
        features = {label: 0 for label in LABELS}
        language_hits = {}
        for locale, rules in LEXICONS.items():
            hits = {label: sum(not negated(text, match) for match in matches(text, phrases)) for label, phrases in rules.items()}
            language_hits[locale] = sum(hits.values())
            for label, count in hits.items():
                features[label] = max(features[label], count)
        inferred = max(language_hits, key=language_hits.get)
        detected = language if language != "auto" else (inferred if language_hits[inferred] else "und")
        recent_negative = any(NEGATIVE.intersection(m.get("analysis", {}).get("labels", [])) for m in history[-3:])
        contextual_sarcasm = bool(features["positive"] and (features["negative"] or features["frustrated"] or
            (recent_negative and re.search(r"\b(?:great|brilliant|perfect|wonderful|genial|super|toll|otimo)\b|वाह", text)
             and not re.search(r"\b(?:fixed|resolved|working now|solved|resuelto|resolu|gelost)\b", text))))
        explicit_sarcasm = features["sarcastic"] > 0
        # A Hindi exclamation alone is not sufficient evidence of sarcasm.
        if matches(text, "वाह|wah|क्या बात है|kya baat hai") and not contextual_sarcasm and not features["negative"] and not recent_negative:
            explicit_sarcasm = False
        scores = {label: 0.05 for label in LABELS}
        for label in ("positive", "negative", "frustrated", "urgent"):
            if features[label]:
                scores[label] = min(0.94, 0.72 + features[label] * 0.06)
        if features["frustrated"]:
            scores["negative"] = max(scores["negative"], 0.76)
        if explicit_sarcasm or contextual_sarcasm:
            scores["sarcastic"] = 0.91 if explicit_sarcasm else 0.74
            scores["negative"] = max(scores["negative"], 0.74)
            scores["positive"] = 0.15
        active = [label for label in LABELS if label != "neutral" and scores[label] >= 0.6]
        if not active:
            active = ["neutral"]
            scores["neutral"] = 0.78 if features["neutral"] else 0.38
        primary = next((label for label in ("sarcastic", "urgent", "frustrated", "negative", "positive") if label in active), "neutral")
        risks, uncertain = [], []
        additional_risks = {
            "account_compromise": r"\baccount\s+(?:(?:may|might|could)\s+be\s+)?(?:hacked|compromised|hijacked)\b|\bsomeone (?:else )?(?:logged|signed) (?:in)?\s*(?:to|into) my account\b|\bcompte (?:a ete |est )?(?:pirate|compromis)\b",
            "duplicate_payment": r"\b(?:charged|billed|debited)\s+(?:me\s+)?(?:twice|two times)\b|\b(?:payment|charge)\s+(?:was |is |has been )?duplicated\b",
            "legal_threat": r"\b(?:threaten|threatening)\s+(?:to sue|legal action)\b",
        }
        for code, phrases in RISKS.items():
            for match in [*matches(text, phrases), *re.finditer(additional_risks[code], text)]:
                if negated(text, match):
                    continue
                prefix = text[max(0, match.start() - 90):match.start()]
                if re.search(r"\b(?:what if|if my|if an|in case|suppose|hypothetical|what happens if|que pasa si)\b", prefix):
                    uncertain.append(code)
                else:
                    risks.append(code)
        return {"primary": primary, "labels": active, "scores": scores, "confidence": scores[primary],
                "language": detected, "method": "multilingual_rules_v1", "confidence_kind": "heuristic_not_calibrated",
                "history_considered": len(history), "contextual_sarcasm": contextual_sarcasm,
                "risk_flags": sorted(set(risks)), "uncertain_risks": sorted(set(uncertain)),
                "needs_clarification": detected == "und" or scores[primary] < 0.6 or bool(uncertain)}


ACKNOWLEDGEMENTS = {
    "ta": ("உங்கள் கருத்துக்கு நன்றி.", "உங்கள் சிரமத்தை புரிந்துகொள்கிறேன்.", "இதற்கு உடனடி கவனம் தேவை என்பதை புரிந்துகொள்கிறேன்.", "விவரித்ததற்கு நன்றி.", "என்ன நடந்தது என்பதை விளக்க முடியுமா?"),
    "te": ("మీ అభిప్రాయానికి ధన్యవాదాలు.", "మీ ఇబ్బందిని అర్థం చేసుకున్నాను.", "దీనికి తక్షణ శ్రద్ధ అవసరమని అర్థమైంది.", "వివరించినందుకు ధన్యవాదాలు.", "ఏమి జరిగిందో వివరించగలరా?"),
    "bn": ("আপনার মতামতের জন্য ধন্যবাদ।", "আপনার অসুবিধা বুঝতে পারছি।", "এটিতে দ্রুত মনোযোগ দেওয়া দরকার বুঝতে পারছি।", "ব্যাখ্যা করার জন্য ধন্যবাদ।", "কী ঘটেছে একটু স্পষ্ট করে বলবেন?"),
    "en": ("Thank you for the feedback.", "I understand this has been frustrating.", "I understand this needs prompt attention.", "Thank you for explaining.", "Could you clarify what happened?"),
    "es": ("Gracias por tus comentarios.", "Entiendo que esto ha sido frustrante.", "Entiendo que esto requiere atención urgente.", "Gracias por explicarlo.", "¿Podrías aclarar qué ocurrió?"),
    "fr": ("Merci pour votre retour.", "Je comprends votre frustration.", "Je comprends que cela nécessite une attention rapide.", "Merci pour ces précisions.", "Pourriez-vous préciser ce qui s'est passé ?"),
    "de": ("Danke für Ihre Rückmeldung.", "Ich verstehe Ihren Frust.", "Ich verstehe, dass dies dringend ist.", "Danke für Ihre Erklärung.", "Können Sie genauer erklären, was passiert ist?"),
    "pt": ("Obrigado pelo retorno.", "Entendo sua frustração.", "Entendo que isto precisa de atenção urgente.", "Obrigado por explicar.", "Poderia esclarecer o que aconteceu?"),
    "hi": ("आपकी प्रतिक्रिया के लिए धन्यवाद।", "मैं आपकी परेशानी समझता हूँ।", "मैं समझता हूँ कि इस पर तुरंत ध्यान देना ज़रूरी है।", "जानकारी देने के लिए धन्यवाद।", "कृपया बताएं कि क्या हुआ?"),
    "ar": ("شكراً على ملاحظاتك.", "أتفهم إحباطك.", "أتفهم أن هذا يحتاج إلى اهتمام عاجل.", "شكراً على التوضيح.", "هل يمكنك توضيح ما حدث؟"),
}


def response_tone(analysis: dict, negative_count: int = 0) -> dict:
    labels = set(analysis["labels"])
    tone, index = ("calm_direct", 2) if analysis["risk_flags"] or "urgent" in labels else (
        ("empathetic", 1) if labels & NEGATIVE or negative_count else
        ("warm", 0) if "positive" in labels else ("neutral", 3))
    if analysis["needs_clarification"] and not analysis["risk_flags"]:
        tone, index = "clarifying", 4
    return {"style": tone, "acknowledgement": ACKNOWLEDGEMENTS.get(analysis["language"], ACKNOWLEDGEMENTS["en"])[index],
            "policy_answer_unchanged": True}


class ConversationWorkflow:
    """Masked conversation state and escalation outbox sharing the ticket clock/transaction."""

    def __init__(self, tickets: TicketWorkflow):
        self.tickets = tickets
        self.access = tickets.access
        self.analyzer = SentimentAnalyzer()
        with tickets._transaction() as db:
            db.execute("CREATE TABLE IF NOT EXISTS conversations (id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS conversation_messages (conversation_id TEXT, message_id TEXT, fingerprint TEXT, PRIMARY KEY(conversation_id, message_id))")
            db.execute("CREATE TABLE IF NOT EXISTS escalations (id TEXT PRIMARY KEY, conversation_id TEXT, condition TEXT, payload TEXT, UNIQUE(conversation_id, condition))")

    def _load(self, db, identifier):
        row = db.execute("SELECT payload FROM conversations WHERE id=?", (identifier,)).fetchone()
        return json.loads(row[0]) if row else None

    def _save(self, db, conversation):
        db.execute("INSERT OR REPLACE INTO conversations VALUES (?, ?)", (conversation["id"], json.dumps(conversation)))

    def _all(self, db):
        return [json.loads(row[0]) for row in db.execute("SELECT payload FROM conversations ORDER BY id")]

    def _summary(self, conversation):
        excerpts = [m["content"][:400] for m in conversation["messages"] if m["role"] == "customer"][-5:]
        return (f"Conversation {conversation['id']}; {conversation['message_count']} customer messages; "
                f"sentiment: {conversation['analysis']['primary']}; negative messages: {conversation['negative_count']}; "
                f"risk flags: {', '.join(conversation['risk_flags']) or 'none'}.\n" + "\n".join(excerpts))

    def _refresh(self, db, conversation, now, revision):
        if conversation["status"] == "resolved":
            return
        calendar = BusinessCalendar(self.tickets.config.tickets)
        opened = calendar.is_open(now)
        conditions = list(conversation["risk_flags"])
        if conversation["negative_count"] >= 2:
            conditions.append("repeated_negative")
        elapsed = max(0, (now - timestamp(conversation["negative_since"])).total_seconds()) if conversation["negative_since"] else 0
        if elapsed > 15 * 60:
            conditions.append("negative_timeout")
        if conversation["urgent"] and not opened:
            conditions.append("urgent_after_hours")
        for condition in conditions:
            details = {"negative_count": conversation["negative_count"], "unresolved_seconds": elapsed,
                       "business_open": opened, "calendar_revision": revision}
            event = {"id": "E-" + uuid.uuid4().hex[:12], "conversation_id": conversation["id"],
                     "reason": REASONS[condition], "activated_condition": condition, "details": details,
                     "created_at": now.isoformat(), "summary": self._summary(conversation),
                     "delivery_status": "pending", "queue": None}
            db.execute("INSERT OR IGNORE INTO escalations VALUES (?, ?, ?, ?)",
                       (event["id"], conversation["id"], condition, json.dumps(event)))
        escalated = bool(db.execute("SELECT 1 FROM escalations WHERE conversation_id=?", (conversation["id"],)).fetchone())
        complaint = bool(conversation["negative_count"] or conversation["urgent"] or conversation["risk_flags"])
        if not complaint:
            queue, scheduled = "none", None
        elif not opened:
            queue = "on_call" if conversation["urgent"] or conversation["risk_flags"] else "next_working_day"
            scheduled = calendar.add_minutes(now, 0).isoformat() if queue == "next_working_day" else None
        else:
            queue, scheduled = ("escalation" if escalated else "support"), None
        conversation.update({"queue": queue, "scheduled_for": scheduled, "escalated": escalated,
                             "assessed_at": now.isoformat(),
                             "status": "escalated" if escalated else "open", "unresolved_negative_seconds": elapsed,
                             "business_open": opened, "calendar_revision": revision,
                             "summary": self._summary(conversation), "tone": response_tone(
                                 {**conversation["analysis"], "risk_flags": conversation["risk_flags"]}, conversation["negative_count"])})
        for row in db.execute("SELECT id,payload FROM escalations WHERE conversation_id=?", (conversation["id"],)).fetchall():
            event = json.loads(row["payload"])
            event.setdefault("initial_queue", queue)
            event["queue"] = queue
            event["scheduled_for"] = scheduled
            db.execute("UPDATE escalations SET payload=? WHERE id=?", (json.dumps(event), row["id"]))
        self._save(db, conversation)

    def _public(self, db, conversation):
        output = json.loads(json.dumps(conversation))
        output["escalations"] = [json.loads(row[0]) for row in db.execute(
            "SELECT payload FROM escalations WHERE conversation_id=? ORDER BY rowid", (conversation["id"],))]
        return output

    def receive(self, payload: Mapping[str, Any], token=None, now=None):
        self.access.authorize(token, "ticket_write")
        if now is not None:
            self.access.authorize(token, "ticket_admin")
        if not isinstance(payload, Mapping) or len(json.dumps(payload)) > 150000:
            raise ValueError("A conversation object of at most 150 KB is required")
        identifier = payload.get("conversation_id") or "C-" + uuid.uuid4().hex[:12]
        if not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", identifier):
            raise ValueError("Conversation ID must be an opaque identifier, not contact information")
        message = payload.get("message")
        if not isinstance(message, str) or not message.strip() or len(message) > 12000:
            raise ValueError("Message must contain between 1 and 12000 characters")
        language = payload.get("language", "auto")
        if not isinstance(language, str) or language not in {"auto", *LEXICONS}:
            raise ValueError("Unsupported language")
        history = payload.get("history", [])
        if not isinstance(history, list) or len(history) > 100:
            raise ValueError("History must be a list of at most 100 messages")
        if any(not isinstance(m, Mapping) or m.get("role") not in {"customer", "user", "agent", "assistant"}
               or not isinstance(m.get("content"), str) or not m["content"].strip() or len(m["content"]) > 12000 for m in history):
            raise ValueError("History entries require a valid role and content")
        for text in [message, *(m["content"] for m in history)]:
            if self.tickets.injection.scan(text).suspicious:
                raise ValueError("Unsafe instructions in conversation content")
        message_id = payload.get("message_id") or "M-" + uuid.uuid4().hex
        if not isinstance(message_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", message_id):
            raise ValueError("Message ID must be an opaque identifier")
        fingerprint = hashlib.sha256(json.dumps([message, history, language, payload.get("customer", "")], sort_keys=True).encode()).hexdigest()
        with self.tickets._transaction() as db:
            revision = self.tickets._policy(db)
            current = self.tickets._now(db, now)
            conversation = self._load(db, identifier)
            duplicate = db.execute("SELECT fingerprint FROM conversation_messages WHERE conversation_id=? AND message_id=?", (identifier, message_id)).fetchone()
            if duplicate:
                if duplicate[0] != fingerprint:
                    raise ValueError("Message ID already used for different content")
                self._refresh(db, conversation, current, revision)
                return self._public(db, conversation)
            if conversation and conversation["status"] == "resolved":
                raise ValueError("Conversation is resolved; start a new conversation")
            if conversation and history:
                raise ValueError("History can only be imported when starting a conversation")
            if conversation and current < timestamp(conversation["updated_at"]):
                raise ValueError("Conversation time cannot move backwards")
            if conversation is None:
                conversation = {"id": identifier, "created_at": current.isoformat(), "updated_at": current.isoformat(),
                                "status": "open", "messages": [], "message_count": 0, "negative_count": 0,
                                "negative_since": None, "risk_flags": [], "urgent": False, "escalated": False}
            previous_time = timestamp(conversation["updated_at"]) if conversation["messages"] else None
            customer = payload.get("customer", "")
            if not isinstance(customer, str) or len(customer) > 200:
                raise ValueError("Customer name must be a string of at most 200 characters")
            for entry in [*history, {"role": "customer", "content": message}]:
                occurred = timestamp(entry["at"]) if entry.get("at") else current
                if occurred > current or (previous_time and occurred < previous_time):
                    raise ValueError("History timestamps must be chronological and not in the future")
                if entry.get("at"):
                    self.access.authorize(token, "ticket_admin")
                previous_time = occurred
                role = "customer" if entry["role"] in {"customer", "user"} else "agent"
                masked = self.tickets._mask(entry["content"], customer)
                masked = re.sub(r"(?<!\w)(?:\d[ -]?){13,19}(?!\w)", "[PAYMENT DATA]", masked)
                masked = re.sub(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b", "[BANK ACCOUNT]", masked)
                masked = re.sub(r"\+\d[\d ()-]{6,20}\d", "[PHONE]", masked)
                masked = re.sub(r"(?im)(?:\b(?:my name is|customer|name|address|cvv|cvc|card number)\s*[:=]?\s*)[^\n;,.]+", "[PERSONAL DATA]", masked)
                saved = {"role": role, "content": masked, "at": occurred.isoformat()}
                if role == "customer":
                    analysis = self.analyzer.analyze(entry["content"], conversation["messages"], language)
                    saved["analysis"] = analysis
                    conversation["analysis"] = analysis
                    conversation["message_count"] += 1
                    if NEGATIVE.intersection(analysis["labels"]):
                        conversation["negative_count"] += 1
                        conversation["negative_since"] = conversation["negative_since"] or occurred.isoformat()
                    conversation["risk_flags"] = sorted(set(conversation["risk_flags"]) | set(analysis["risk_flags"]))
                    conversation["urgent"] = conversation["urgent"] or "urgent" in analysis["labels"]
                conversation["messages"].append(saved)
                conversation["messages"] = conversation["messages"][-100:]
            conversation["updated_at"] = current.isoformat()
            self._refresh(db, conversation, current, revision)
            db.execute("INSERT INTO conversation_messages VALUES (?, ?, ?)", (identifier, message_id, fingerprint))
            return self._public(db, conversation)

    def _snapshot(self, db, now):
        conversations = [self._public(db, c) for c in self._all(db)]
        escalations = [json.loads(row[0]) for row in db.execute("SELECT payload FROM escalations ORDER BY rowid DESC LIMIT 200")]
        return {"conversations": sorted(conversations, key=lambda c: c["updated_at"], reverse=True), "escalations": escalations,
                "clock": {"mode": "simulated" if self.tickets._meta(db, "clock") else "live", "now": now.isoformat()},
                "calendar": {"timezone": self.tickets.config.tickets.timezone, "business_start": self.tickets.config.tickets.business_start,
                             "business_end": self.tickets.config.tickets.business_end, "revision": self.tickets._meta(db, "policy")["revision"]},
                "counts": {"open": sum(c["status"] != "resolved" for c in conversations),
                           "escalated": sum(c["escalated"] and c["status"] != "resolved" for c in conversations),
                           "on_call": sum(c["queue"] == "on_call" for c in conversations),
                           "scheduled": sum(c["queue"] == "next_working_day" for c in conversations)}}

    def snapshot(self, token=None):
        self.access.authorize(token, "ticket_read")
        with self.tickets._transaction() as db:
            revision = self.tickets._policy(db)
            now = self.tickets._now(db)
            for conversation in self._all(db):
                self._refresh(db, conversation, now, revision)
            return self._snapshot(db, now)

    def tick(self, now=None):
        # Internal worker; public time-changing routes require ticket_admin.
        with self.tickets._transaction() as db:
            revision = self.tickets._policy(db)
            current = self.tickets._now(db, now)
            for conversation in self._all(db):
                self._refresh(db, conversation, current, revision)
            return self._snapshot(db, current)

    def resolve(self, identifier, token=None):
        self.access.authorize(token, "ticket_write")
        with self.tickets._transaction() as db:
            conversation = self._load(db, identifier)
            if not conversation:
                raise ValueError("Conversation not found")
            if conversation["status"] == "resolved":
                return self._public(db, conversation)
            now = self.tickets._now(db)
            self._refresh(db, conversation, now, self.tickets._policy(db))
            conversation.update({"status": "resolved", "queue": "none", "scheduled_for": None, "resolved_at": now.isoformat()})
            self._save(db, conversation)
            for row in db.execute("SELECT id,payload FROM escalations WHERE conversation_id=?", (identifier,)).fetchall():
                event = json.loads(row["payload"])
                event.update({"delivery_status": "closed", "closed_at": now.isoformat()})
                db.execute("UPDATE escalations SET payload=? WHERE id=?", (json.dumps(event), row["id"]))
            return self._public(db, conversation)
