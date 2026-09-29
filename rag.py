from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone
from typing import Any
from urllib.parse import urlencode
import json
import re

from .config import RagConfig
from .knowledge_metadata import iso_date, record_metadata, version_key
from .models import DocumentRecord, QueryResult, RetrievedContext
from .retrieval import Retriever, tokenize
from .security import PromptInjectionProtector, SensitiveDataMasker


QUESTION_WORDS = {
    "i", "my", "me", "we", "our", "you", "your", "please", "tell", "about", "explain", "give",
    "do", "doe", "did", "can", "could", "would", "should", "will", "need", "get", "have", "has",
    "what", "policy", "polici", "window", "period", "current", "currently", "latest", "today",
    "historical", "active", "applicable", "apply", "appli", "was", "were", "used", "specified",
    "say", "says", "state", "date", "as", "of", "on", "during", "long", "many", "much", "fix", "solve", "resolve",
    "a", "the", "an", "is", "are", "for", "to", "in", "at", "and", "information", "detail",
}


def normalized_text(text: str) -> str:
    return " ".join(text.casefold().split()).strip(' "\u201c\u201d')


def statements(text: str) -> list[str]:
    lines = []
    for line in text.splitlines():
        if line.lstrip().startswith(("#", "```")):
            continue
        line = re.sub(r"^\s*(?:[-*]\s+|\d+[.)]\s+)", "", line).strip()
        if line:
            lines.append(line)
    text = " ".join(lines)
    return [part.strip() for part in re.split(r"(?<=[.!?])\s+(?=[A-Z0-9])", text) if part.strip()]


def query_terms(question: str, product: str | None, region: str | None) -> set[str]:
    text = re.sub(r"\b\d{4}-\d{2}-\d{2}\b", " ", question)
    for value in (product, region):
        if value and value != "*":
            text = re.sub(r"(?<!\w)" + re.escape(value) + r"(?!\w)", " ", text, flags=re.I)
    return set(tokenize(text)) - QUESTION_WORDS


def verify_claims(answer: str, contexts: list[RetrievedContext]) -> list[str]:
    """Fail closed: every answer line must be a complete, cited source statement."""
    allowed = {context.citation_id: normalized_text(context.chunk) for context in contexts}
    unsupported = []
    for claim in answer.splitlines():
        if not claim.strip():
            continue
        refs = re.findall(r"\[(S\d+)\]", claim)
        text = re.sub(r"\[S\d+\]", "", claim).strip()
        if not refs or any(ref not in allowed for ref in refs) or any(normalized_text(text) != allowed[ref] for ref in refs):
            unsupported.append(claim)
    return unsupported or ([] if answer.strip() else ["Empty answer"])


class KnowledgeAssistant:
    def __init__(self, config: RagConfig):
        self.config = config
        self.injection = PromptInjectionProtector()
        self.masker = SensitiveDataMasker()

    def _stop(self, status, message, version_id, day=None, unsupported=None):
        return QueryResult(answer=message, confidence=0.0, contexts=[], active_version_id=version_id,
                           escalated=True, status=status, as_of=day.isoformat() if day else None,
                           clarification_request=message if status == "needs_clarification" else "",
                           unsupported_claims=unsupported or [])

    def _date(self, question, as_of, now):
        today = (now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now).astimezone(timezone.utc).date()
        mentioned = set(re.findall(r"\b\d{4}-\d{2}-\d{2}\b", question))
        if len(mentioned) > 1 or re.search(r"\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b", question):
            raise ValueError("Please specify one policy date using YYYY-MM-DD.")
        if as_of and mentioned and str(as_of) not in mentioned:
            raise ValueError("The selected date and question disagree. Please confirm the policy date.")
        historical = as_of is not None or bool(mentioned)
        raw = as_of or (next(iter(mentioned)) if mentioned else today.isoformat())
        try:
            day = iso_date(raw)
        except ValueError:
            raise ValueError("Please specify a valid policy date using YYYY-MM-DD.") from None
        if not historical and re.search(r"\bhistoric(?:al)?\b|\blast (?:year|month)\b|\bprevious(?:ly)?\b|\bused to\b|\bin (?:19|20)\d{2}\b", question, re.I):
            raise ValueError("Which date should I use for the historical policy? Please specify YYYY-MM-DD.")
        if day > today:
            raise ValueError("That date is in the future. Please choose today or a historical date.")
        return day, day < today

    def authorized_documents(self, documents, role):
        output = []
        levels = self.config.access_by_role.get(role, ())
        for document in documents:
            try:
                metadata = record_metadata(document, self.config.legacy_access_level)
            except (ValueError, TypeError):
                continue
            if metadata["access_level"] not in levels:
                continue
            if document.metadata.get("_current_access_level", metadata["access_level"]) not in levels:
                continue
            material = json.dumps(metadata) + "\n" + document.masked_content + "\n" + "\n".join(document.chunks)
            if self.injection.scan(material).score >= 2:
                continue
            output.append(replace(document, metadata=metadata))
        return output

    def _applicable(self, documents, day, historical):
        eligible, families = [], {}
        for document in documents:
            meta = document.metadata
            if meta["effective_date"] is None:
                if not historical:
                    eligible.append(document)
                continue
            if iso_date(meta["effective_date"]) > day:
                continue
            if meta["document_type"] == "policy":
                key = (meta["policy_id"].casefold(), meta["product"].casefold(), meta["region"].casefold())
                families.setdefault(key, []).append(document)
            elif not meta["expiry_date"] or day < iso_date(meta["expiry_date"]):
                eligible.append(document)
        for family in families.values():
            latest = max((d.metadata["effective_date"], version_key(d.metadata["document_version"])) for d in family)
            # A superseded policy must not reappear when its successor expires.
            eligible.extend(d for d in family if (d.metadata["effective_date"], version_key(d.metadata["document_version"])) == latest
                            and (not d.metadata["expiry_date"] or day < iso_date(d.metadata["expiry_date"])))
        return eligible

    def answer(self, documents: list[DocumentRecord], question: str, role: str, version_id: str | None,
               product: str | None = None, region: str | None = None, as_of: str | None = None,
               now: datetime | None = None, top_k: int | None = None, proposed_answer: str | None = None) -> QueryResult:
        if not isinstance(question, str) or not question.strip() or len(question) > 10000:
            return self._stop("needs_clarification", "Please provide a specific knowledge-base question.", version_id)
        if self.injection.scan(question).score >= 2:
            return self._stop("refused", "I cannot follow instructions that override access or grounding controls.", version_id)
        for value in (product, region):
            if value is not None and (not isinstance(value, str) or len(value) > 120):
                raise ValueError("Product and region must be strings of at most 120 characters")
        if top_k is not None and (type(top_k) is not int or not 1 <= top_k <= 20):
            raise ValueError("top_k must be an integer between 1 and 20")
        try:
            day, historical = self._date(question, as_of, now or datetime.now(timezone.utc))
        except ValueError as exc:
            return self._stop("needs_clarification", str(exc), version_id)
        authorized = self.authorized_documents(documents, role)
        applicable = self._applicable(authorized, day, historical)
        requested = {"product": (product or "").strip(), "region": (region or "").strip()}
        for key in requested:
            if requested[key] == "*":
                requested[key] = ""
            if not requested[key]:
                mentioned = {d.metadata[key] for d in applicable if d.metadata[key] != "*" and re.search(
                    r"(?<!\w)" + re.escape(d.metadata[key]) + r"(?!\w)", question, re.I)}
                if len(mentioned) == 1:
                    requested[key] = mentioned.pop()
        applicable = [d for d in applicable if all(not value or d.metadata[key].casefold() in {"*", value.casefold()} for key, value in requested.items())]
        terms = query_terms(question, requested["product"], requested["region"])
        if not terms:
            return self._stop("needs_clarification", "Which policy, product fact, or troubleshooting issue do you need?", version_id, day)
        relevant = [d for d in applicable if terms & set(tokenize(d.masked_content + " " + d.title + " " + d.metadata["topic"]))]
        if not relevant:
            return self._stop("insufficient_evidence", "I could not find authorised, applicable evidence for that question and date.", version_id, day)
        for key, value in requested.items():
            options = {d.metadata[key].casefold() for d in relevant if d.metadata[key] != "*"}
            if not value and len(options) > 1:
                return self._stop("needs_clarification", f"Please specify the {key} so I can select the applicable information.", version_id, day)
        groups = {}
        for document in relevant:
            meta = document.metadata
            family = meta["topic"] or meta["policy_id"]
            if family:
                groups.setdefault(family.casefold(), []).append(document)
        excluded = set()
        for group in groups.values():
            policies = [d for d in group if d.metadata["document_type"] == "policy"]
            if not policies:
                continue
            for document in group:
                if document not in policies:
                    excluded.add(document.path)
            def specificity(document):
                return tuple(int(bool(requested[key]) and document.metadata[key] != "*") for key in ("product", "region"))
            winners = [d for d in policies if not any(
                all(a >= b for a, b in zip(specificity(other), specificity(d))) and specificity(other) != specificity(d)
                for other in policies)]
            excluded.update(d.path for d in policies if d not in winners)
            if len({normalized_text(d.masked_content) for d in winners}) > 1:
                return self._stop("needs_clarification", "Equally applicable sources disagree. Please clarify the policy or ask support to resolve the conflict.", version_id, day)
        relevant = [d for d in relevant if d.path not in excluded]
        policies = [d for d in relevant if d.metadata["document_type"] == "policy"]
        for document in relevant:
            if document.metadata["document_type"] == "policy":
                continue
            addressed = terms & set(tokenize(document.masked_content))
            if addressed and any(addressed <= (terms & set(tokenize(policy.masked_content))) for policy in policies):
                excluded.add(document.path)
        relevant = [d for d in relevant if d.path not in excluded]
        passages = [replace(d, chunks=statements(self.masker.mask(d.masked_content)[0])) for d in relevant]
        all_contexts = Retriever(passages).search(" ".join(sorted(terms)), sum(len(d.chunks) for d in passages) or 1)
        if not all_contexts:
            return self._stop("insufficient_evidence", "I could not find a source statement that supports an answer.", version_id, day)
        supported_terms = set(tokenize(" ".join(c.chunk for c in all_contexts)))
        if terms - supported_terms:
            return self._stop("insufficient_evidence", "The available evidence does not support all parts of that question. Please clarify or provide another source.", version_id, day)
        # Check conflicts before top-k, so reducing the result count cannot hide a contradiction.
        for index, first in enumerate(all_contexts):
            for second in all_contexts[index + 1:]:
                if self._contradicts(first.chunk, second.chunk, terms):
                    return self._stop("needs_clarification", "The applicable evidence contains conflicting claims. Please ask support to confirm the correct information.", version_id, day)
        limit = min(top_k or self.config.max_contexts, self.config.max_contexts)
        contexts, seen = [], set()
        for context in all_contexts:
            if normalized_text(context.chunk) in seen:
                continue
            seen.add(normalized_text(context.chunk))
            context.citation_id = f"S{len(contexts) + 1}"
            contexts.append(context)
            if len(contexts) == limit:
                break
        covered = set(tokenize(" ".join(c.chunk for c in contexts)))
        if terms - covered or any(len(c.chunk) > 4000 for c in contexts):
            return self._stop("insufficient_evidence", "Please narrow the question so its answer can be fully supported by the retrieved sources.", version_id, day)
        answer = "\n\n".join(f"{c.chunk} [{c.citation_id}]" for c in contexts)
        if proposed_answer is not None:
            if not isinstance(proposed_answer, str) or len(proposed_answer) > 20000:
                raise ValueError("The proposed answer must be text of at most 20000 characters")
            unsupported = verify_claims(proposed_answer, contexts)
            if unsupported:
                return self._stop("unsupported_claim", "The proposed answer contains uncited or unsupported claims. I cannot present it as fact.",
                                  version_id, day, [self.masker.mask(claim)[0] for claim in unsupported])
            answer = proposed_answer
        if verify_claims(answer, contexts):
            return self._stop("unsupported_claim", "The answer failed source verification.", version_id, day)
        citations = [{"id": c.citation_id, "path": c.path, "quote": c.chunk, "content_hash": c.content_hash,
                      "knowledge_base_version": version_id, **c.metadata,
                      "url": "/api/rag/source?" + urlencode({"path": c.path, "version_id": version_id or "", "content_hash": c.content_hash, "as_of": day.isoformat()})}
                     for c in contexts]
        return QueryResult(answer=answer, confidence=1.0, contexts=contexts, active_version_id=version_id,
                           citations=citations, status="answered", as_of=day.isoformat())

    def _contradicts(self, first, second, terms):
        left, right = set(tokenize(first)), set(tokenize(second))
        if not (terms & left and terms & right):
            return False
        numbers_a = set(re.findall(r"\b\d+(?:\.\d+)?\b", first))
        numbers_b = set(re.findall(r"\b\d+(?:\.\d+)?\b", second))
        negations = {"not", "no", "never", "unavailable", "prohibited", "cannot"}
        core_a, core_b = left - numbers_a - negations, right - numbers_b - negations
        overlap = len(core_a & core_b) / max(1, min(len(core_a), len(core_b)))
        if overlap < .7:
            return False
        return bool((numbers_a and numbers_b and numbers_a != numbers_b) or (bool(left & negations) != bool(right & negations)))
