from __future__ import annotations

from difflib import get_close_matches
from datetime import date
from typing import Any
import re
import unicodedata

from .config import SessionConfig
from .security import SensitiveDataMasker
from .sentiment import LEXICONS, matches, normalize


LANGUAGE_NAMES = {"en": "English", "es": "Spanish", "fr": "French", "de": "German", "pt": "Portuguese",
                  "hi": "Hindi / Hinglish", "ar": "Arabic", "ta": "Tamil", "te": "Telugu", "bn": "Bengali"}
INTENTS = {
    "order_status": {
        "en": "where is|track|tracking|delivery|shipment|arrived|late|order status",
        "es": "donde esta|seguimiento|entrega|llegado", "fr": "ou est|suivi|livraison|arrive",
        "de": "wo ist|verfolgen|lieferung|angekommen", "pt": "onde esta|rastrear|entrega|chegou",
        "hi": "कहाँ|कहां|कब आएगा|नहीं आया|kahan|kab aayega|nahi aaya",
        "ar": "اين|أين|تتبع|توصيل|لم يصل",
        "ta": "எங்கே|எப்போது வரும்|வரவில்லை|டெலிவரி|விநியோகம்|எப்போது கிடைக்கும்|enga|enge|eppo varum|eppodhu varum|varala|varavillai|delivery",
        "te": "ఎక్కడ|ఎప్పుడు వస్తుంది|రాలేదు|డెలివరీ|ekkada|eppudu vastundi|raledu|delivery",
        "bn": "কোথায়|কোথায়|কখন আসবে|আসেনি|ডেলিভারি|kothay|kothai|kokhon ashbe|kokhon asbe|aseni|delivery",
    },
    "refund": {
        "en": "refund|refunds|return|returns|money back", "es": "reembolso|devolver|devolucion", "fr": "remboursement|rembourser|retour",
        "de": "ruckerstattung|zuruckgeben|retoure", "pt": "reembolso|devolucao",
        "hi": "पैसे वापस|वापसी|रिफंड|paise wapas|refund", "ar": "استرداد|ارجاع|إرجاع",
        "ta": "பணம் திரும்ப|பணத்தைத் திரும்ப|பணத்தை திரும்ப|திருப்பி|ரீஃபண்ட்|ரீபண்ட்|panam thirumba|panathai thirumba|thiruppi|refund",
        "te": "వాపసు|వాపసు కావాలి|డబ్బు తిరిగి|రిఫండ్|dabbu tirigi|dabbulu tirigi|vapas|refund",
        "bn": "ফেরত|রিফান্ড|টাকা ফেরত|taka ferot|taka ferat|ferot|refund",
    },
    "cancellation": {
        "en": "cancel|cancellation", "es": "cancelar|cancelacion", "fr": "annuler|annulation", "de": "stornieren|stornierung",
        "pt": "cancelar|cancelamento", "hi": "रद्द|रद्द करो|radd|cancel", "ar": "الغاء|إلغاء",
        "ta": "ரத்து|ரத்துசெய்|rathu|raththu|cancel", "te": "రద్దు|raddu|cancel", "bn": "বাতিল|batil|cancel",
    },
    "payment": {
        "en": "payment|charged|charge|invoice|paid|billing", "es": "pago|cobro|factura", "fr": "paiement|facture|debite",
        "de": "zahlung|rechnung|abgebucht", "pt": "pagamento|fatura|cobrado", "hi": "भुगतान|पैसे कट|payment|bhugtan",
        "ar": "دفع|فاتورة|خصم", "ta": "கட்டணம்|பணம் பிடித்தம்|இருமுறை|ரசீது|kattanam|panam eduthanga|irumurai|payment",
        "te": "చెల్లింపు|రెండుసార్లు|బిల్లు|chellimpu|rendusarlu|bill", "bn": "পেমেন্ট|দুবার|বিল|duibar|dubar|bill",
    },
    "technical": {
        "en": "error|not working|broken|login|log in|password|crash", "es": "error|no funciona|contrasena",
        "fr": "erreur|ne fonctionne pas|mot de passe", "de": "fehler|funktioniert nicht|passwort",
        "pt": "erro|nao funciona|senha", "hi": "काम नहीं|त्रुटि|पासवर्ड|kaam nahi|password",
        "ar": "خطأ|لا يعمل|كلمة المرور",
        "ta": "பிழை|வேலை செய்யவில்லை|கடவுச்சொல்|velai seyyala|velai seiyala|pizhai|error",
        "te": "లోపం|పనిచేయడం లేదు|పాస్‌వర్డ్|panicheyatledu|panicheyadam ledu|error",
        "bn": "ত্রুটি|কাজ করছে না|পাসওয়ার্ড|kaj korche na|kaj korchhe na|error",
    },
    "product_info": {
        "en": "specifications|compatible|warranty|product information", "es": "garantia|compatible", "fr": "garantie|compatible",
        "de": "garantie|kompatibel", "pt": "garantia|compativel", "hi": "वारंटी|उत्पाद जानकारी|warranty",
        "ar": "ضمان|مواصفات", "ta": "உத்தரவாதம்|விவரங்கள்|utharavadham|warranty",
        "te": "వారంటీ|వివరాలు|vivaralu|warranty", "bn": "ওয়ারেন্টি|বিবরণ|biboron|warranty",
    },
}
MARKERS = {
    "en": "hello|please|my|the|is|want|need|what|when|thanks|actually|instead|and",
    "es": "hola|quiero|necesito|por favor|mi|gracias", "fr": "bonjour|je|voudrais|mon|merci",
    "de": "hallo|ich|meine|bitte|danke", "pt": "ola|quero|meu|obrigado",
    "hi": "mera|meri|mujhe|chahiye|hai|karo|कृपया|मेरा|चाहिए",
    "ar": "مرحبا|من فضلك|اريد|أريد", "ta": "vanakkam|enakku|ennoda|enoda|venum|vendum|illai|enna|irukku|pannunga|நான்|எனக்கு|வேண்டும்|என்",
    "te": "namaskaram|naaku|naku|naa|kavali|undi|cheyandi|enti|నాకు|కావాలి|నా",
    "bn": "nomoskar|amar|ami|chai|chayi|ache|korun|hobe|dhonnobad|আমার|আমি|চাই",
}
SCRIPTS = {"hi": (0x0900, 0x097F), "bn": (0x0980, 0x09FF), "ta": (0x0B80, 0x0BFF),
           "te": (0x0C00, 0x0C7F), "ar": (0x0600, 0x06FF)}
LANGUAGE_ALIASES = {"en": "english|अंग्रेजी", "es": "spanish|espanol", "fr": "french|francais", "de": "german|deutsch",
                    "pt": "portuguese|portugues", "hi": "hindi|हिंदी", "ar": "arabic|العربية",
                    "ta": "tamil|தமிழ்|தமிழில்", "te": "telugu|తెలుగు|తెలుగులో", "bn": "bengali|bangla|বাংলা|বাংলায়"}
FOLLOWUPS = "it|that|that one|and that|what about it|and this|same one|அது|அதற்கு|அதுவும்|adhu|athu|అది|దాని|adi|daaniki|সেটা|ওটা|seta|ota|और वह|uska|eso|cela|das"
CORRECTIONS = "actually|correction|correct|instead|sorry|திருத்தம்|உண்மையில்|பதிலாக|thirutham|సవరణ|నిజానికి|బదులుగా|nijaaniki|সংশোধন|আসলে|বদলে|asole|ashole|दरअसल|गलत|en realidad"
NEGATIONS = r"(?:not|isn't|non|no|இல்லை|illa|illai|కాదు|kaadu|kadu|নয়|নয়|noy|नहीं)"

REPLIES = {
    "en": {"clarify": "Which order or request do you mean? Please clarify.", "language": "Which language would you like to use? Please rephrase your request.", "updated": "I have updated the details in this conversation.", "greeting": "Hello. How can I help?", "switched": "I will continue in your selected language.", "source": "The policy source says:", "missing": "I do not have enough verified information to answer that request. Please clarify the product or issue.", "unavailable": "The knowledge service is unavailable. Please try again.", "restored": "Welcome back. Your previous conversation summary has been restored."},
    "ta": {"clarify": "எந்த ஆர்டர் அல்லது கோரிக்கையை குறிப்பிடுகிறீர்கள்? தயவுசெய்து விளக்கவும்.", "language": "எந்த மொழியில் பேச விரும்புகிறீர்கள்? உங்கள் கோரிக்கையை மீண்டும் விளக்கவும்.", "updated": "இந்த உரையாடலில் விவரங்களைத் திருத்தியுள்ளேன்.", "greeting": "வணக்கம். உங்களுக்கு எப்படி உதவலாம்?", "switched": "நீங்கள் தேர்ந்தெடுத்த மொழியில் தொடர்கிறேன்.", "source": "கொள்கை ஆவணத்தில் உள்ள தகவல்:", "missing": "பதில் அளிக்க போதுமான சரிபார்க்கப்பட்ட தகவல் இல்லை. தயாரிப்பு அல்லது சிக்கலை தெளிவாக குறிப்பிடவும்.", "unavailable": "தகவல் சேவை தற்போது கிடைக்கவில்லை. மீண்டும் முயற்சிக்கவும்.", "restored": "மீண்டும் வருக. முந்தைய உரையாடலின் சுருக்கம் மீட்டெடுக்கப்பட்டது."},
    "te": {"clarify": "మీరు ఏ ఆర్డర్ లేదా అభ్యర్థనను సూచిస్తున్నారు? దయచేసి వివరించండి.", "language": "మీరు ఏ భాషలో మాట్లాడాలనుకుంటున్నారు? మీ అభ్యర్థనను మళ్లీ వివరించండి.", "updated": "ఈ సంభాషణలో వివరాలను సవరించాను.", "greeting": "నమస్కారం. మీకు ఎలా సహాయపడగలను?", "switched": "మీరు ఎంచుకున్న భాషలో కొనసాగిస్తాను.", "source": "విధాన పత్రంలోని సమాచారం:", "missing": "సమాధానం ఇవ్వడానికి తగిన ధృవీకరించిన సమాచారం లేదు. ఉత్పత్తి లేదా సమస్యను స్పష్టంగా వివరించండి.", "unavailable": "సమాచార సేవ అందుబాటులో లేదు. దయచేసి మళ్లీ ప్రయత్నించండి.", "restored": "తిరిగి స్వాగతం. మీ మునుపటి సంభాషణ సారాంశం పునరుద్ధరించబడింది."},
    "bn": {"clarify": "আপনি কোন অর্ডার বা অনুরোধের কথা বলছেন? অনুগ্রহ করে স্পষ্ট করুন।", "language": "আপনি কোন ভাষায় কথা বলতে চান? অনুগ্রহ করে আপনার অনুরোধটি আবার বলুন।", "updated": "এই কথোপকথনে তথ্য সংশোধন করেছি।", "greeting": "নমস্কার। কীভাবে সাহায্য করতে পারি?", "switched": "আপনার নির্বাচিত ভাষায় কথা চালিয়ে যাব।", "source": "নীতির নথিতে বলা আছে:", "missing": "উত্তর দেওয়ার জন্য যথেষ্ট যাচাই করা তথ্য নেই। পণ্য বা সমস্যাটি স্পষ্ট করে বলুন।", "unavailable": "তথ্য পরিষেবা এখন উপলব্ধ নয়। আবার চেষ্টা করুন।", "restored": "আবার স্বাগতম। আপনার আগের কথোপকথনের সারাংশ ফিরিয়ে আনা হয়েছে।"},
}
# Existing languages retain their acknowledgement packs; these are session-specific clarifications.
for code, clarify, missing in [
    ("es", "¿A qué pedido o solicitud te refieres? Por favor, acláralo.", "No tengo suficiente información verificada. Aclara el producto o problema."),
    ("fr", "De quelle commande ou demande parlez-vous ? Merci de préciser.", "Je n'ai pas assez d'informations vérifiées. Précisez le produit ou le problème."),
    ("de", "Welche Bestellung oder Anfrage meinen Sie? Bitte erläutern Sie dies.", "Es fehlen verifizierte Informationen. Bitte nennen Sie das Produkt oder Problem."),
    ("pt", "A qual pedido ou solicitação se refere? Por favor, esclareça.", "Não tenho informação verificada suficiente. Esclareça o produto ou problema."),
    ("hi", "आप किस ऑर्डर या अनुरोध की बात कर रहे हैं? कृपया स्पष्ट करें।", "पर्याप्त सत्यापित जानकारी नहीं है। कृपया उत्पाद या समस्या स्पष्ट करें।"),
    ("ar", "أي طلب تقصد؟ يرجى التوضيح.", "لا تتوفر معلومات موثقة كافية. يرجى توضيح المنتج أو المشكلة."),
]:
    REPLIES[code] = {**REPLIES["en"], "clarify": clarify, "missing": missing, "language": clarify}

QUERIES = {"order_status": "What is the delivery policy?", "refund": "What is the refund window?",
           "cancellation": "What is the cancellation policy?", "payment": "What is the duplicate payment policy?",
           "technical": "How do I troubleshoot this error?", "product_info": "What is the product warranty?"}
ENTITY_KEYS = ("names", "order_ids", "dates", "product_codes")
ENTITY_PATTERNS = {
    "names": r"(?:my name is|name\s*[:=]|customer\s*[:=]|என் பெயர்|எனது பெயர்|నా పేరు|আমার নাম|मेरा नाम)\s*([^\n,;.!?।]{1,80})",
    "order_ids": r"(?<!\w)(?:ORD(?:ER)?|PO)[-_][A-Za-z0-9-]{1,60}(?!\w)|(?:order(?:\s*(?:id|number|no\.?))?|ஆர்டர்|ఆర్డర్|অর্ডার|ऑर्डर)\s*[:#=]?\s*([A-Za-z0-9][A-Za-z0-9_-]{2,63})",
    "dates": r"(?<!\d)(?:\d{4}-\d{2}-\d{2}|\d{1,2}[/-]\d{1,2}[/-]\d{4}|\d{1,2}\s+(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+\d{4})(?!\d)",
    "product_codes": r"(?<!\w)(?:SKU|PROD|ERR)[-_][A-Za-z0-9-]{1,60}(?!\w)|(?:product(?: code)?|sku|தயாரிப்பு குறியீடு|ఉత్పత్తి కోడ్|পণ্যের কোড)\s*[:#=]?\s*([A-Za-z0-9][A-Za-z0-9_-]{2,63})",
}


def extract_entities(message: str, supplied: dict | None = None) -> tuple[dict, str]:
    supplied = supplied or {}
    if not isinstance(supplied, dict) or not set(supplied) <= set(ENTITY_KEYS):
        raise ValueError("Entity fields must be names, order_ids, dates or product_codes")
    found = {key: [] for key in ENTITY_KEYS}
    protected = []
    for key, pattern in ENTITY_PATTERNS.items():
        for match in re.finditer(pattern, message, re.I):
            value = match[1] if match.lastindex else match[0]
            if key in {"order_ids", "product_codes"} and not any(c.isdigit() for c in value):
                continue
            if key == "names" and any(c.isdigit() for c in value):
                continue
            protected.append(value)
            prefix = normalize(message[max(0, match.start() - 20):match.start()])
            suffix = normalize(message[match.end():match.end() + 15])
            denied = re.search(rf"\b{NEGATIONS}\s*$", prefix) or re.match(rf"\s*{NEGATIONS}(?:\s|[,.;]|$)", suffix)
            if not denied and value not in found[key]:
                found[key].append(value)
        values = supplied.get(key, [])
        if not isinstance(values, list) or len(values) > 10 or any(not isinstance(v, str) or not v.strip() or len(v) > 120 for v in values):
            raise ValueError("Each entity field must contain up to ten nonempty strings of at most 120 characters")
        for value in values:
            if value not in found[key]:
                found[key].append(value)
            protected.append(value)
    prepared = message
    for value in sorted(set(protected), key=len, reverse=True):
        prepared = prepared.replace(value, " __entity__ ")
    return found, prepared


def safe_history(message: str, entities: dict) -> str:
    placeholders = {}
    for value in sorted({v for values in entities.values() for v in values}, key=len, reverse=True):
        placeholder = f"[ENTITY_{len(placeholders)}]"
        placeholders[placeholder] = value
        message = message.replace(value, placeholder)
    message = SensitiveDataMasker().mask(message)[0]
    message = re.sub(r"(?im)\b(?:card|cvv|cvc|address|account number)\s*[:=]\s*[^\n;]+", "[REDACTED]", message)
    for placeholder, value in placeholders.items():
        message = message.replace(placeholder, value)
    return message


class LanguageInterpreter:
    def __init__(self, config: SessionConfig):
        self.config = config
        self.vocabulary = sorted({word for rules in INTENTS.values() for phrases in rules.values()
                                  for phrase in phrases.split("|") for word in normalize(phrase).split()
                                  if re.fullmatch(r"[a-z]{5,}", word)})

    def interpret(self, message: str, memory: dict, language="auto", supplied=None) -> dict[str, Any]:
        if language != "auto" and language not in self.config.languages:
            raise ValueError("Selected language is not enabled")
        entities, prepared = extract_entities(message, supplied)
        for values in memory.get("entities", {}).values():
            for value in sorted(values, key=len, reverse=True):
                prepared = prepared.replace(value, " __entity__ ")
        text = normalize(prepared)
        corrections = []
        def spell(match):
            word = match[0]
            if word in self.vocabulary:
                return word
            choices = get_close_matches(word, self.vocabulary, n=2, cutoff=.83)
            if len(choices) == 1:
                corrections.append({"typed": word, "interpreted": choices[0]})
                return choices[0]
            return word
        text = re.sub(r"(?<!\w)[a-z]{4,}(?!\w)", spell, text)
        scores = {}
        for locale in LANGUAGE_NAMES:
            score = len(matches(text, MARKERS[locale])) * 2
            phrases = {p for rules in INTENTS.values() for p in rules[locale].split("|")}
            for phrase in phrases:
                if matches(text, phrase):
                    # Loanwords shared with English carry intent but little language evidence.
                    score += .5 if locale != "en" and re.fullmatch(r"[a-z ]+", phrase) and any(phrase in r["en"].split("|") for r in INTENTS.values()) else 2
            if locale in SCRIPTS:
                start, end = SCRIPTS[locale]
                letters = sum(start <= ord(char) <= end and unicodedata.category(char)[0] in {"L", "M"} for char in text)
                if letters:
                    score += min(16, letters / 2 + 3)
            scores[locale] = score
        best = max(scores, key=scores.get)
        detected = [locale for locale, score in scores.items() if score >= 2]
        maximum = scores[best]
        competing = sorted(scores.values(), reverse=True)[1]
        language_confidence = min(.97, .56 + maximum * .055) if maximum else .1
        if maximum and maximum == competing:
            language_confidence = min(language_confidence, .55)
        followup = bool(matches(text.strip(" .?!"), FOLLOWUPS)) and len(text.split()) <= 7
        entity_reply = bool(any(entities.values()) and memory.get("intents") and
                            "multiple_entities_need_selection" in memory.get("pending_clarification", []))
        prior_language = memory.get("language", self.config.default_language)
        if maximum < 2 and (followup or entity_reply) and memory.get("intents"):
            best, language_confidence = prior_language, .72
            detected = [best]
        requested = None
        switching = bool(re.search(r"\b(?:speak|reply|respond|continue|switch|use|in)\b|பேச|தமிழில்|చెప్ప|తెలుగులో|বলুন|বাংলায়|में", text))
        if switching:
            requested = next((locale for locale, aliases in LANGUAGE_ALIASES.items() if matches(text, aliases)), None)
        disabled = (best not in self.config.languages and maximum >= 2) or (requested and requested not in self.config.languages)
        preference = requested or (language if language != "auto" else memory.get("preferred_language"))
        disabled = disabled or bool(preference and preference not in self.config.languages)
        output_language = preference or (best if maximum >= 2 else prior_language)
        if requested and not disabled:
            language_confidence = .99
        if output_language not in self.config.languages:
            output_language = self.config.default_language
        requests = []
        for intent, rules in INTENTS.items():
            count = sum(len(matches(text, rules[locale])) for locale in self.config.languages)
            if count:
                requests.append({"intent": intent, "confidence": .86 if not corrections else .74, "query": QUERIES[intent]})
        correcting = bool(matches(text, CORRECTIONS))
        ambiguous_entities = any(len(values) > 1 for key, values in entities.items() if key in {"order_ids", "product_codes"})
        if correcting:
            entities = {key: values[-1:] if values else values for key, values in entities.items()}
            ambiguous_entities = False
        if not requests and followup and len(memory.get("intents", [])) == 1 and not memory.get("ambiguous_entities"):
            intent = memory["intents"][0]
            requests = [{"intent": intent, "confidence": .70, "query": QUERIES[intent], "from_context": True}]
        if not requests and entity_reply and not ambiguous_entities:
            requests = [{"intent": intent, "confidence": .8, "query": QUERIES[intent], "from_context": True} for intent in memory["intents"]]
        if requests:
            kind, intent_confidence = "requests", min(r["confidence"] for r in requests)
        elif correcting and any(entities.values()):
            kind, intent_confidence = "correction", .95
        elif requested:
            kind, intent_confidence = "language_switch", .98
        elif any(matches(text, LEXICONS[locale]["positive"] + "|" + GREETINGS[locale]) for locale in self.config.languages):
            kind, intent_confidence = "greeting", .8
        else:
            kind, intent_confidence = "unknown", .2
        ambiguous_followup = followup and not requests
        reasons = []
        if disabled or language_confidence < self.config.language_threshold:
            reasons.append("language_uncertain_or_disabled")
        if intent_confidence < self.config.intent_threshold or ambiguous_followup:
            reasons.append("intent_uncertain")
        if ambiguous_entities:
            reasons.append("multiple_entities_need_selection")
        policy_date = None
        historical = bool(re.search(r"\b(?:as of|what was|policy on|policy in|back then)\b|அன்று|అప్పటి|সেদিন", text))
        if historical and entities["dates"]:
            candidate = entities["dates"][0]
            try:
                if len(entities["dates"]) != 1 or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", candidate):
                    raise ValueError("Ambiguous policy date")
                date.fromisoformat(candidate)
                policy_date = candidate
            except ValueError:
                reasons.append("policy_date_needs_clarification")
        for clause in re.split(r"\s+(?:and|also|மேலும்|மற்றும்|మరియు|এবং)\s+", text):
            known = any(matches(clause, rules[locale]) for rules in INTENTS.values() for locale in self.config.languages)
            if requests and not known and "__entity__" not in clause and re.search(r"\b(?:do|explain|tell|check|help|want|need)\b|செய்ய|చేయ|করুন", clause):
                reasons.append("partial_intent_uncertain")
                intent_confidence = min(intent_confidence, .3)
        return {"language": output_language, "detected_languages": detected, "mixed_language": len(detected) > 1,
                "preferred_language": preference,
                "policy_date": policy_date,
                "current_policy": bool(re.search(r"\b(?:current|today)\b|இன்றைய|నేటి|আজকের", text)),
                "language_confidence": round(language_confidence, 3), "intent_confidence": intent_confidence,
                "kind": kind, "requests": requests, "entities": entities, "correcting": correcting,
                "ambiguous_entities": ambiguous_entities, "spelling_interpretations": corrections,
                "needs_clarification": bool(reasons), "clarification_reasons": reasons,
                "method": "offline_language_rules_v1", "confidence_kind": "heuristic_not_calibrated"}


GREETINGS = {"en": "hello|hi", "es": "hola", "fr": "bonjour", "de": "hallo", "pt": "ola", "hi": "नमस्ते|namaste",
             "ar": "مرحبا", "ta": "வணக்கம்|vanakkam", "te": "నమస్కారం|namaskaram", "bn": "নমস্কার|nomoskar"}
